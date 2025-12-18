from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncGenerator, AsyncIterator, Dict, List, Optional
from dataclasses import dataclass

from src.services.service_factory import Authenticator, ThreadStorage, McpManager

from src.services.streaming.litellm_client import acomplete, first_text
from src.services.streaming.stream_variants import (
    SVUser, SVAssistant, SVCode,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    StreamVariant,
    help_convert_sv_ccrm,
    from_json_to_sv
)
from src.services.streaming.tool_calls import run_tool_via_mcp, accumulate_tool_calls, finalize_tool_calls, parse_tool_result, FinalSummary
from src.core.heartbeat import heartbeat_content
from src.core.available_chatbots import model_supports_images
from src.core.logging_setup import configure_logging

from src.services.streaming.active_conversations import (
    ConversationState, get_conversation_state, 
    get_conv_mcpmanager, get_conv_messages,
    add_to_conversation, initialize_conversation,
    register_tool_task, unregister_tool_task,   
)

DEFAULT_LOGGER = configure_logging(__name__)


@dataclass
class StreamState:
    user_invoked: bool = True
    tool_call: Optional[Dict[str, Any]] = None 
    finished: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Streaming with tools
# ──────────────────────────────────────────────────────────────────────────────

async def stream_with_tools(
    *,
    model: str,
    thread_id: str,
    messages: List[Dict[str, Any]], # system_prompt
    acomplete_func=acomplete,
    stream_state: StreamState = None,
    logger=None,
) -> AsyncIterator[StreamVariant]:
    log = logger or DEFAULT_LOGGER
    
    # Append the conversation history to system prompt
    conv_sv = await get_conv_messages(thread_id)
    msg_hist = help_convert_sv_ccrm(conv_sv, include_images=model_supports_images(model), include_meta=False)
    messages.extend(msg_hist)

    # Get MCPManager of the conversation
    mcp = await get_conv_mcpmanager(thread_id)

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
    
    if accumulated_asst_text:
        asst_v = SVAssistant(text="".join(accumulated_asst_text))
        await add_to_conversation(thread_id, [asst_v])

    # If no tool calls, wrap up everything and return
    if not tool_calls:
        end_v = SVStreamEnd(message="Stream ended.")
        yield end_v
        stream_state.finished = True
        # await add_to_conversation(thread_id, [end_v])
        return

    # 3) Run tools
    id = ""
    for tc in tool_calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        name = (tc.get("function") or {}).get("name", "")
        id = tc.get("id", id)
        args_txt = (tc.get("function") or {}).get("arguments", "")

        async def run_with_heartbeat():
            """Run the tool while periodically sending heartbeats."""
            tool_task = asyncio.create_task(run_tool_via_mcp(
                mcp=mcp,
                tool_name=name,
                arguments_json=args_txt,
                logger=log,
            ))

            await register_tool_task(thread_id, tool_task)

            try:
                # While tool runs, emit heartbeats every few seconds
                while not tool_task.done():
                    hb = await heartbeat_content()
                    yield hb
                    await asyncio.sleep(10)  # heartbeat interval (seconds)

                # When done, return the final result text
                result_text = await tool_task
                yield result_text
            
            except asyncio.CancelledError:
                # /stop or connection close has cancelled this task
                tool_task.cancel()
                raise

            except Exception as e:
                tool_task.cancel()
                raise

            finally:
                # Ensure the task is removed from the registry when it finishes
                await unregister_tool_task(thread_id, tool_task)

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
        tc_variants : List[StreamVariant] = []

        if name == "code_interpreter":
            # We append accumulated code text to thread
            code_v = SVCode(code=args_txt, id=id)
            tc_variants.append(code_v)

        tool_out_v: List[StreamVariant] = []
        tool_msgs: List[Dict[str, Any]] = []
        # Parsing tool call output as StreamVariants and messages to model
        for r in parse_tool_result(result_text, tool_name=name, call_id=id):
            if isinstance(r, FinalSummary):
                tool_out_v, tool_msgs, = r.var_block, r.tool_messages
                break
            else:
                yield r  # Streaming the result to endpoint

        tc_variants.extend(tool_out_v)
        await add_to_conversation(thread_id, tc_variants)

        if tool_msgs:
            messages.extend(tool_msgs)


# ──────────────────────────────────────────────────────────────────────────────
# High-level orchestrator (storage-agnostic)
# ──────────────────────────────────────────────────────────────────────────────

async def run_stream(
    *,
    model: str,
    thread_id: Optional[str],
    user_input: str,
    system_prompt: List[Dict[str, Any]],
    logger=None,
) -> AsyncGenerator[StreamVariant, None]:
    """
    Orchestrate a single turn, yielding StreamVariant objects.
    """
    log = logger or DEFAULT_LOGGER
    # Append ServerHint with thread_id
    hint = SVServerHint(data={"thread_id": thread_id})
    yield hint
    # Append user content
    user_v = SVUser(text=user_input or "")
    # await add_to_conversation(thread_id, [hint, user_v])
    await add_to_conversation(thread_id, [user_v])

    stream_state = StreamState()
    
    # Stream model/tool output
    while not stream_state.finished:
        conv_state = await get_conversation_state(thread_id)
        if conv_state != ConversationState.STREAMING:
            break
        try:
            async for piece in stream_with_tools(
                thread_id=thread_id,
                messages=system_prompt,
                model=model,
                acomplete_func=acomplete,
                stream_state=stream_state,
                logger=log,
            ):  
                yield piece

        except asyncio.CancelledError:
            end_v = SVStreamEnd(message="Cancelled.")
            log.error("Stream is cancelled.")
            # await add_to_conversation(thread_id, [end_v])
        except Exception as e:
            log.exception("Stream error: %s", e)
            err_v = SVServerError(message=str(e))
            end_v = SVStreamEnd(message="Stream ended with an error.")
            # await add_to_conversation(thread_id, [err_v, end_v])
            await add_to_conversation(thread_id, [err_v])
            stream_state.finished = True
            yield err_v
            yield end_v


async def prepare_for_stream(
    thread_id, 
    user_id,
    Auth: Optional[Authenticator] = None, 
    Storage: Optional[ThreadStorage] = None,
    read_history: Optional[bool] = False, 
    logger=None,
) :
    """ 
    Preparations for the streaming, read history (if needed), add to Registry and 
    set conversation state to "streaming
    """
    log = logger or DEFAULT_LOGGER
    messages: List[Dict[str, Any]] = []
    if read_history and Storage:
        try:
            messages = await get_conversation_history(thread_id, Storage)
        except Exception as e:
            msg = f"Prompt/history assembly failed: {e}"
            log.exception(msg)
            err = SVServerError(message=msg)
            return err

    # Check if the conversation already exists in registry
    # If not initialize it, and add the first messages 
    await initialize_conversation(thread_id, user_id, messages=messages, auth=Auth, logger=log)
    return None


async def get_conversation_history(
    thread_id: Optional[str],
    Storage: ThreadStorage,
    ):
    # Build messages for ongoing conversation
    prior_json: List[dict] = await Storage.read_thread(thread_id)
    prior_sv: List[StreamVariant] = [from_json_to_sv(item) for item in prior_json]
    return prior_sv


__all__ = ["stream_with_tools", "run_stream"]
