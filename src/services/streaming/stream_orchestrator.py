from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Dict, List, Optional

try:
    from fastapi import Request  # only for typing; safe to be None in headless runs
except Exception:  # pragma: no cover
    Request = Any  # type: ignore

from src.services.mcp.mcp_manager import McpManager
from src.services.streaming.litellm_client import acomplete, first_text
from src.services.streaming.stream_variants import (
    SVAssistant,
    SVPrompt,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVUser,
    StreamVariant,
    help_convert_sv_ccrm,
)
from src.core.available_chatbots import model_supports_images
from src.core.prompting import get_entire_prompt, get_entire_prompt_json

log = logging.getLogger(__name__)

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
    request: Optional[Request],
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
    if name == "get_context_from_resources":
        res = mcp.call_tool(
            "rag",
            session_key=session_key,
            name="get_context_from_resources",
            arguments={
                "question": args.get("question", ""),
                "resources_to_retrieve_from": args.get("resources_to_retrieve_from", ""),
            },
            extra_headers={
                "x-freva-vault-url": request.headers.get("x-freva-vault-url", "") if request else "",
                "x-freva-rest-url":  request.headers.get("x-freva-rest-url", "") if request else "",
            },
        )
        return json.dumps(res)

    # default fallback
    res = mcp.call_tool(
        "default",
        session_key=session_key,
        name=name,
        arguments=args,
        extra_headers={
            "x-freva-vault-url": request.headers.get("x-freva-vault-url", "") if request else "",
            "x-freva-rest-url":  request.headers.get("x-freva-rest-url", "") if request else "",
        },
    )
    return json.dumps(res)

# ──────────────────────────────────────────────────────────────────────────────
# Streaming with tools
# ──────────────────────────────────────────────────────────────────────────────

async def stream_with_tools(
    request: Optional[Request],
    *,
    model: str,
    messages: List[Dict[str, Any]],
    mcp: McpManager,
    acomplete_func=acomplete,
    session_key_override: Optional[str] = None,   # ← NEW: allows per-thread sessions
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
                    for p in re.findall(r"\S+\s*", piece):
                        if p:
                            yield p

                if delta.get("tool_calls"):
                    _accumulate_tool_calls({"choices": [{"delta": delta}]}, tool_agg)

                if choice.get("finish_reason"):
                    break
        else:
            full_txt = first_text(resp) or ""
            for p in re.findall(r"\S+\s*", full_txt):
                if p:
                    yield p

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
    for tc in tool_calls:
        name = (tc.get("function") or {}).get("name", "")
        args_json = (tc.get("function") or {}).get("arguments", "")
        try:
            result_text = await _run_tool_via_mcp(
                request=request, mcp=mcp, name=name, arguments_json=args_json, session_key=session_key
            )
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
    request: Optional[Request] = None,
    mcp: Optional[McpManager] = None,
    # storage hooks:
    persist: Optional[PersistFn] = None,          # called with [SVPrompt?] and [hint,user], then with [assistant,end] or [error,end]
    load_thread: Optional[LoadThreadFn] = None,   # when continuing a thread, returns prior SVs to be converted
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

    # Build messages: new thread -> system prompt; else -> convert prior conversation
    try:
        if create_new:
            base = get_entire_prompt(user_id, thread_id, model)
            prompt_json = get_entire_prompt_json(user_id, thread_id, model)
            if persist:
                await persist([SVPrompt(payload=prompt_json)])
            messages = list(base)
        else:
            prior_sv: List[StreamVariant] = load_thread(thread_id) if load_thread else []
            messages = help_convert_sv_ccrm(
                prior_sv, include_images=model_supports_images(model), include_meta=False
            )
    except Exception as e:
        msg = f"prompt/history assembly failed: {e}"
        log.exception(msg)
        err = SVServerError(message=msg)
        end = SVStreamEnd(message="Error")
        if persist:
            await persist([err, end])
        yield err
        yield end
        return

    # Append user content and persist hint+user
    messages.append({"role": "user", "content": user_input or ""})
    hint = SVServerHint(data={"thread_id": thread_id})
    user_v = SVUser(text=user_input or "")
    if persist:
        await persist([hint, user_v])
    yield hint
    yield user_v

    # Stream model/tool output
    accumulated: List[str] = []
    chunk_count = 0
    try:
        mgr = mcp or McpManager()
        async for piece in stream_with_tools(
            request,
            model=model,
            messages=messages,
            mcp=mgr,
            acomplete_func=acomplete,
            session_key_override=thread_id,   # ← HERE: per-thread MCP session
        ):
            accumulated.append(piece)
            chunk_count += 1
            yield SVAssistant(text=piece)

        final_text = "".join(accumulated)
        assistant_v = SVAssistant(text=final_text)
        end_v = SVStreamEnd(message="Done")
        if persist:
            await persist([assistant_v, end_v])
        yield end_v

    except asyncio.CancelledError:
        final_text = "".join(accumulated)
        assistant_v = SVAssistant(text=final_text)
        end_v = SVStreamEnd(message="Cancelled")
        if persist:
            await persist([assistant_v, end_v])
    except Exception as e:
        log.exception("stream error: %s", e)
        err_v = SVServerError(message=str(e))
        end_v = SVStreamEnd(message="Error")
        if persist:
            await persist([err_v, end_v])
        yield err_v
        yield end_v


__all__ = ["stream_with_tools", "run_stream", "new_conversation_id"]
