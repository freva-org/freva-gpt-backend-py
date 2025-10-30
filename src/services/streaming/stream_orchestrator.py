from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Dict, List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from ansi2html import Ansi2HTMLConverter

from src.services.mcp.mcp_manager import McpManager
from src.services.streaming.litellm_client import acomplete, first_text
from src.services.streaming.stream_variants import (
    SVAssistant,
    SVPrompt,
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
)
from src.core.available_chatbots import model_supports_images
from src.core.prompting import get_entire_prompt
from src.services.storage.router import append_thread, read_thread

log = logging.getLogger(__name__)

conv = Ansi2HTMLConverter(inline=True) # Jupyter sends the stdout or stderr as a string containing ANSI escape sequences (color codes). We parse them as html messages

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def new_conversation_id(length: int = 32) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

# ──────────────────────────────────────────────────────────────────────────────
# Tool-call accumulation helpers (OpenAI-style deltas)
# ──────────────────────────────────────────────────────────────────────────────

def _accumulate_tool_calls(delta: Dict[str, Any], agg: Dict[str, Any]) -> None:
    choices = delta.get("choices") or []
    if not choices:
        return
    d = choices[0].get("delta") or {}
    tc_list = d.get("tool_calls") or []
    if not tc_list:
        return

    store: Dict[int, Dict[str, Any]] = agg.setdefault("by_index", {})  # type: ignore
    for item in tc_list:
        idx = item.get("index")
        if idx is None:
            continue
        entry = store.setdefault(idx, {"type": "function", "function": {"name": "", "arguments": ""}})
        if item.get("id"):
            entry["id"] = item["id"]
        f = item.get("function") or {}
        if f.get("name"):
            entry["function"]["name"] = f["name"]
        if f.get("arguments"):
            entry["function"]["arguments"] = entry["function"].get("arguments", "") + f["arguments"]

def _finalize_tool_calls(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = agg.get("by_index") or {}
    out: List[Dict[str, Any]] = []
    for idx in sorted(store.keys()):
        tc = store[idx]
        fn = tc.get("function") or {}
        tc.setdefault("type", "function")
        tc["function"] = {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")}
        out.append(tc)
    return out

# ──────────────────────────────────────────────────────────────────────────────
# MCP tool runner
# ──────────────────────────────────────────────────────────────────────────────

async def _run_tool_via_mcp(
    *,
    mcp: McpManager,
    name: str,
    arguments_json: str,
    session_key: str,
) -> str:
    try:
        args = json.loads(arguments_json or "{}")
    except Exception:
        args = {"_raw": arguments_json}

    # bespoke mapping preserved (extend as needed)
    #TODO: standardize this call
    if name == "get_context_from_resources":
        res = mcp.call_tool(
            "rag",
            session_key=session_key,
            name="get_context_from_resources",
            arguments={
                "question": args.get("question", ""),
                "resources_to_retrieve_from": args.get("resources_to_retrieve_from", ""),
            },
        )
        return json.dumps(res)
    elif name=="code_interpreter":
        res = mcp.call_tool(
            "code",
            session_key=session_key,
            name="code_interpreter",
            arguments={
                "code": args.get("code", ""),
            },
        )
        return json.dumps(res)

    # default fallback
    res = mcp.call_tool(
        "default",
        session_key=session_key,
        name=name,
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
) -> AsyncIterator[str]:
    # 1) First request
    tool_agg: Dict[str, Any] = {}
    tools = mcp.openai_tools() if hasattr(mcp, "openai_tools") else []
    kwargs = {"model": model, "messages": messages, "stream": True}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp1 = await acomplete_func(**kwargs)

    async def _handle_stream(resp) -> AsyncIterator[str]:
        if hasattr(resp, "__aiter__"):
            async for chunk in resp:  # type: ignore
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}

                piece = delta.get("content") or ""
                if piece:
                    yield SVAssistant(text=piece)

                tc_list = delta.get("tool_calls") or []
                if tc_list:
                    _accumulate_tool_calls({"choices": [{"delta": delta}]}, tool_agg)
                    tool_name = tool_agg.get("by_index")[0].get("function").get("name") if tool_agg else None
                    for tc in tc_list:
                        fn = tc.get("function") or {}
                        args_chunk = fn.get("arguments")
                        if args_chunk and tool_name=="code_interpreter":
                            tool_id = tc.get("id") or ""
                            # stream arguments chunk immediately
                            yield SVCode(code=args_chunk, call_id=tool_id)

                if choice.get("finish_reason"):
                    break
        else:
            full_txt = first_text(resp) or ""
            for p in re.findall(r"\S+\s*", full_txt):
                if p:
                    yield SVAssistant(text=full_txt)

    async for p in _handle_stream(resp1):
        yield p

    # 2) Any tool calls?
    tool_calls = _finalize_tool_calls(tool_agg)
    if not tool_calls:
        return

    # 3) Run tools
    # Use per-thread session if provided, else fall back to previous behaviour
    session_key = session_key_override or (messages[0].get("content", "") if messages else "")
    tool_result_messages: List[Dict[str, Any]] = []
    id = ""
    for tc in tool_calls:
        name = (tc.get("function") or {}).get("name", "")
        id = tc.get("id", id)
        args_txt = (tc.get("function") or {}).get("arguments", "")
        try:
            result_text = await _run_tool_via_mcp(
                mcp=mcp, name=name, arguments_json=args_txt, session_key=session_key
            )
            if name =="code_interpreter":
                # Accumulated code text
                code = json.loads(args_txt or "{}").get("code", "")
                code_v = SVCode(code=code, call_id=id)
                code_block = [code_v]
                # Code output: display data, image or error?        
                result = json.loads(result_text).get("structuredContent", "")
                # Printed/displayed output
                out = result["stdout"] + "\n\n" + result["result_repr"]
                if out.strip('\n'):
                    codeout_v = SVCodeOutput(output=out, call_id=id)
                    code_block.append(codeout_v)
                    yield codeout_v
                # Image/html etc., rich output
                rich_out = result["display_data"]
                if rich_out:
                    for r in rich_out:
                        if "image/png" in r.keys():
                            image_v = SVImage(b64=r["image/png"])
                            code_block.append(image_v)
                            yield image_v
                        if "application/json" in r.keys():
                            #TODO: check this output, is having two codeoutput variant with the same id okay?
                            json_v = SVCodeOutput(output=r["application/json"], call_id=id)
                            code_block.append(json_v)
                            yield json_v
                # Error
                err = result.get("stderr", "") + "\n\n" + result.get("error", "")
                if err.strip('\n'):
                    err = conv.convert(err)
                    err_v = SVCodeError(message=err, call_id=id)
                    code_block.append(err_v)
                    yield err_v
                await append_thread(thread_id, user_id, code_block, database)
        except Exception as e:
            log.exception("Tool %s failed", name)
            result_text = json.dumps({"error": str(e)})

        tool_result_messages.append(
            {"role": "tool", "tool_call_id": tc.get("id", ""), "name": name, "content": result_text}
        )

    # 4) Second request with tool results
    second_messages = list(messages)
    second_messages.append({"role": "assistant", "tool_calls": tool_calls})
    second_messages.extend(tool_result_messages)

    kwargs2 = {"model": model, "messages": second_messages, "stream": True}
    if tools:
        kwargs2["tools"] = tools
        kwargs2["tool_choice"] = "auto"
    resp2 = await acomplete_func(**kwargs2)

    async for p in _handle_stream(resp2):
        yield p
    # TODO retry is skipped here

# ──────────────────────────────────────────────────────────────────────────────
# High-level orchestrator (storage-agnostic)
# ──────────────────────────────────────────────────────────────────────────────

PersistFn = Callable[[List[StreamVariant]], Awaitable[None]]
LoadThreadFn = Callable[[str], List[StreamVariant]]  # returns a Conversation (list of SV*)

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
    Storage is delegated to the caller via `persist` and `load_thread`.
    Also ensures MCP session key = resolved thread_id for clean per-conversation sessions.
    """
    if not thread_id:
        thread_id = new_conversation_id()
        create_new = True
    else:
        create_new = False

    # Build messages
    try:
        system_prompt = get_entire_prompt(user_id, thread_id, model)
        if create_new:
            # New thread
            hint = SVServerHint(data={"thread_id": thread_id})
            yield hint
            await append_thread(thread_id, user_id, [hint], database)
            # Start with the system prompt
            messages = list(system_prompt)
        else:
            prior_sv: List[StreamVariant] = await read_thread(thread_id, database)
            # We strip the threads from system prompt before saving, so we need to start with that then append prior conversation
            messages = list(system_prompt)
            messages.extend(
                help_convert_sv_ccrm(prior_sv, include_images=model_supports_images(model), include_meta=False)
                )
    except Exception as e:
        msg = f"Prompt/history assembly failed: {e}"
        log.exception(msg)
        err = SVServerError(message=msg)
        end = SVStreamEnd(message="Stream ended with an error.")
        await append_thread(thread_id, user_id,[err, end], database)
        yield err
        yield end
        return

    # Append user content
    messages.append({"role": "user", "content": user_input or ""})
    user_v = SVUser(text=user_input or "")
    await append_thread(thread_id, user_id, [user_v], database)
    
    # Stream model/tool output
    p_type_check = None
    streamed_v: List[StreamVariant] = []
    accumulated: List[str] = []
    try:
        mgr = mcp or McpManager()
        async for piece in stream_with_tools(
            thread_id=thread_id,
            user_id=user_id,
            database=database,
            model=model,
            messages=messages,
            mcp=mgr,
            acomplete_func=acomplete,
            session_key_override=thread_id,   # per-thread MCP session
        ):  
            yield piece
            # Accumulate all the streamed assistant test and append
            if isinstance(piece, (SVCodeOutput, SVCodeError, SVImage, SVCode)) and accumulated:
                final = "".join(accumulated)
                accumulated = []
                if p_type_check == SVAssistant:
                    streamed_v.append(SVAssistant(text=final))
            elif isinstance(piece, SVAssistant):
                accumulated.append(piece.text)
                p_type_check = SVAssistant

        if accumulated:
            final_text = "".join(accumulated)
            assistant_v = SVAssistant(text=final_text)
            streamed_v.append(assistant_v)
        
        end_v = SVStreamEnd(message="Done")
        streamed_v.append(end_v)
        await append_thread(thread_id, user_id, streamed_v, database)
        yield end_v

    except asyncio.CancelledError:
        final_text = "".join(accumulated)
        assistant_v = SVAssistant(text=final)
        end_v = SVStreamEnd(message="Cancelled.")
        await append_thread(thread_id, user_id, [assistant_v, end_v], database)
    except Exception as e:
        log.exception("stream error: %s", e)
        err_v = SVServerError(message=str(e))
        end_v = SVStreamEnd(message="Stream ended with an error.")
        await append_thread(thread_id, user_id, [err_v, end_v], database)
        yield err_v
        yield end_v


__all__ = ["stream_with_tools", "run_stream", "new_conversation_id"]
