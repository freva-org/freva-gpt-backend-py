from __future__ import annotations

import asyncio
import json
import logging

import re
from typing import Any, AsyncGenerator, AsyncIterator, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from dataclasses import dataclass

from src.services.mcp.mcp_manager import McpManager
from src.services.streaming.litellm_client import acomplete, first_text
from src.services.streaming.stream_variants import (
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVCodeError,
    SVImage,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVUser,
    StreamVariant,
    help_convert_sv_ccrm,
    from_json_to_sv
)
from src.services.streaming.helpers import new_conversation_id, accumulate_tool_calls, finalize_tool_calls, parse_tool_result, FinalSummary
from src.services.streaming.heartbeat import heartbeat_content
from src.core.available_chatbots import model_supports_images
from src.core.prompting import get_entire_prompt
from src.services.storage.router import append_thread, read_thread

log = logging.getLogger(__name__)


@dataclass
class StreamState:
    user_invoked: bool = True
    tool_call: Optional[Dict[str, Any]] = None 
    finished: bool = False

# ──────────────────────────────────────────────────────────────────────────────
# MCP tool runner
# ──────────────────────────────────────────────────────────────────────────────

async def _run_tool_via_mcp(
    *,
    mcp: McpManager,
    tool_name: str,
    arguments_json: str,
    session_key: str,
) -> str:
    try:
        args = json.loads(arguments_json or "{}")
    except Exception:
        args = {"_raw": arguments_json}

    server_name = mcp.get_server_from_tool(tool_name)

    res = mcp.call_tool(
        server_name,
        session_key=session_key,
        name=tool_name,
        arguments=args,
    )
    return json.dumps(res)

# ──────────────────────────────────────────────────────────────────────────────
# Streaming with tools
# ──────────────────────────────────────────────────────────────────────────────

async def stream_with_tools(
    *,
    model: str,
    thread_id: str,
    user_id: str,
    database: Optional[AsyncIOMotorDatabase] = None,
    messages: List[Dict[str, Any]],
    mcp: McpManager,
    acomplete_func=acomplete,
    session_key_override: Optional[str] = None,   # allows per-thread sessions
    stream_state: StreamState = None,
) -> AsyncIterator[StreamVariant]:
    # 1) First request
    tool_agg: Dict[str, Any] = {}
    tools = mcp.openai_tools() if hasattr(mcp, "openai_tools") else []
    kwargs = {"model": model, "messages": messages, "stream": True}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    resp = await acomplete_func(**kwargs)

    accumulated_asst_text: List[str] = []

    if hasattr(resp, "__aiter__"):
        call_id = ""
        async for chunk in resp:  # type: ignore
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}

            # assistant text
            piece = delta.get("content") or ""
            if piece:
                accumulated_asst_text.append(piece)
                yield SVAssistant(text=piece)

            # tool call: stream code chunks live and accumulate deltas
            tc_list = delta.get("tool_calls") or []
            if tc_list:
                accumulate_tool_calls({"choices": [{"delta": delta}]}, tool_agg)
                tool_name = tool_agg.get("by_index")[0].get("function").get("name") if tool_agg else None
                for tc in tc_list:
                    fn = tc.get("function") or {}
                    call_id = tc.get("id", call_id)
                    args_chunk = fn.get("arguments", "")
                    if args_chunk and tool_name=="code_interpreter":
                        # stream arguments chunk immediately
                        yield SVCode(code=args_chunk, id=call_id)

            #  end-of-message
            if choice.get("finish_reason"):
                break
    else:
        full_txt = first_text(resp) or ""
        for p in re.findall(r"\S+\s*", full_txt):
            if p:
                accumulated_asst_text.append(full_txt)
                yield SVAssistant(text=full_txt)

    # 2) Any tool calls?
    tool_calls = finalize_tool_calls(tool_agg)
    
    # If no tool calls, wrap up everything and return
    if not tool_calls:
        end_v = SVStreamEnd(message="Stream ended.")
        yield end_v
        if accumulated_asst_text:
            asst_v = SVAssistant(text="".join(accumulated_asst_text))
            await append_thread(thread_id, user_id, [asst_v, end_v], database)
        if stream_state is not None:
            stream_state.finished = True
        return

    # 3) Run tools
    # Use per-thread session if provided, else fall back to previous behaviour
    session_key = session_key_override or (messages[0].get("content", "") if messages else "")
    id = ""
    for tc in tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        name = (tc.get("function") or {}).get("name", "")
        id = tc.get("id", id)
        args_txt = (tc.get("function") or {}).get("arguments", "")

        async def run_with_heartbeat():
            """Run the tool while periodically sending heartbeats."""
            tool_task = asyncio.create_task(_run_tool_via_mcp(
                mcp=mcp, tool_name=name, arguments_json=args_txt, session_key=session_key
            ))
            try:
                # While tool runs, emit heartbeats every few seconds
                while not tool_task.done():
                    hb = await heartbeat_content()
                    yield hb
                    await asyncio.sleep(3)  # adjust heartbeat interval (seconds)

                # When done, return the final result text
                result_text = await tool_task
                yield result_text
            except Exception as e:
                tool_task.cancel()
                raise

        try:
            result_text = None
            heartbeats_v: List[StreamVariant] = []
            async for item in run_with_heartbeat():
                if isinstance(item, SVServerHint):
                    yield item  # Stream heartbeat ServerHint variants
                    heartbeats_v.append(item)
                elif isinstance(item, str):
                    # The function returns the final tool result as last value
                    result_text = item 
        except Exception as e:
            log.exception("Tool %s failed", name)
            result_text = json.dumps({"error": str(e)})

        # We will collect tool input and output as Stream Variants and append to thread
        toolcall_variants : List[StreamVariant] = []

        if name == "code_interpreter":
            # We append accumulated code text to thread
            code_json = json.loads(args_txt or "{}").get("code", "")
            code_v = SVCode(code=code_json, id=id)
            toolcall_variants.append(code_v)

        tool_out_v: List[StreamVariant] = []
        tool_msgs: List[Dict[str, Any]] = []
        # Parsing tool call output as StreamVariants and messages to model
        for r in parse_tool_result(result_text, tool_name=name, call_id=id):
            if isinstance(r, FinalSummary):
                tool_out_v, tool_msgs, = r.var_block, r.tool_messages
                break
            else:
                yield r  # Streaming the result to endpoint

        toolcall_variants.extend(tool_out_v)
        await append_thread(thread_id, user_id, toolcall_variants, database)

        if tool_msgs:
            messages.extend(tool_msgs)


# ──────────────────────────────────────────────────────────────────────────────
# High-level orchestrator (storage-agnostic)
# ──────────────────────────────────────────────────────────────────────────────

async def run_stream(
    *,
    model: str,
    thread_id: Optional[str],
    user_id: str,
    user_input: str,
    database: Optional[AsyncIOMotorDatabase] = None,
    mcp: Optional[McpManager] = None,
) -> AsyncGenerator[StreamVariant, None]:
    """
    Orchestrate a single turn, yielding StreamVariant objects.
    Also ensures MCP session key = resolved thread_id for clean per-conversation sessions.
    """
    create_new = False
    if not thread_id:
        thread_id = new_conversation_id()
        create_new = True
        
    async for m in build_messages(model, create_new, thread_id, user_id, database):
        if isinstance(m, List):
            messages = m
        else:
            yield m

    STATE = StreamState()
    mgr = mcp or McpManager()
    
    # Stream model/tool output
    while not STATE.finished:
        hint = SVServerHint(data={"thread_id": thread_id})
        yield hint
        await append_thread(thread_id, user_id, [hint], database)
        if STATE.user_invoked:
            # Append user content
            messages.append({"role": "user", "content": user_input or ""})
            user_v = SVUser(text=user_input or "")
            await append_thread(thread_id, user_id, [user_v], database)
            STATE.user_invoked = False
        try:
            async for piece in stream_with_tools(
                thread_id=thread_id,
                user_id=user_id,
                database=database,
                model=model,
                messages=messages,
                mcp=mgr,
                acomplete_func=acomplete,
                session_key_override=thread_id,   # per-thread MCP session
                stream_state=STATE,
            ):  
                yield piece

        except asyncio.CancelledError:
            end_v = SVStreamEnd(message="Cancelled.")
            await append_thread(thread_id, user_id, [ end_v], database)
        except Exception as e:
            log.exception("stream error: %s", e)
            err_v = SVServerError(message=str(e))
            end_v = SVStreamEnd(message="Stream ended with an error.")
            await append_thread(thread_id, user_id, [err_v, end_v], database)
            yield err_v
            yield end_v


__all__ = ["stream_with_tools", "run_stream", "new_conversation_id"]


async def build_messages(
    model: str,
    create_new: bool,
    thread_id: Optional[str],
    user_id: str,
    database: Optional[AsyncIOMotorDatabase] = None,
    ):
    # Build messages for ongoing conversation
    try:
        system_prompt = get_entire_prompt(user_id, thread_id, model)
        
        if create_new:
            # New thread
            # Start with the system prompt
            messages = list(system_prompt)
        else:
            prior_json: List[dict] = await read_thread(thread_id, database)
            prior_sv: List[StreamVariant] = [from_json_to_sv(item) for item in prior_json]
            # We strip the threads from system prompt before saving, so we need to start with that then append prior conversation
            messages = list(system_prompt)
            messages.extend(
                help_convert_sv_ccrm(prior_sv, include_images=model_supports_images(model), include_meta=False)
                )
        yield messages
    except Exception as e:
        msg = f"Prompt/history assembly failed: {e}"
        log.exception(msg)
        err = SVServerError(message=msg)
        end = SVStreamEnd(message="Stream ended with an error.")
        await append_thread(thread_id, user_id,[err, end], database)
        yield err
        yield end
        return
