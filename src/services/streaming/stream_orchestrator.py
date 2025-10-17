import json
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Request

from src.services.mcp.mcp_manager import McpManager


# --------- helpers: assemble tool_calls from streaming deltas -----------------

def _accumulate_tool_calls(delta: Dict[str, Any], agg: Dict[str, Any]) -> None:
    choices = delta.get("choices") or []
    if not choices:
        return
    d = choices[0].get("delta") or {}

    if "content" in d and d["content"] is not None:
        agg.setdefault("content", "")
        agg["content"] += d["content"]

    if "tool_calls" in d and d["tool_calls"]:
        agg.setdefault("tool_calls", [])
        for i, tc_delta in enumerate(d["tool_calls"]):
            while len(agg["tool_calls"]) <= i:
                agg["tool_calls"].append({"id": None, "type": None, "function": {"name": None, "arguments": ""}})
            slot = agg["tool_calls"][i]
            if tc_delta.get("id") is not None:
                slot["id"] = tc_delta["id"]
            if tc_delta.get("type") is not None:
                slot["type"] = tc_delta["type"]
            fdelta = tc_delta.get("function") or {}
            if fdelta.get("name") is not None:
                slot["function"]["name"] = fdelta["name"]
            if fdelta.get("arguments") is not None:
                slot["function"]["arguments"] = slot["function"].get("arguments", "") + fdelta["arguments"]

def _finalize_tool_calls(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    tcs = agg.get("tool_calls") or []
    out = []
    for tc in tcs:
        f = tc.get("function") or {}
        if f.get("name") and isinstance(f.get("arguments"), str):
            out.append(tc)
    return out


# --------- run a single tool via MCP -----------------------------------------

async def _run_tool_via_mcp(
    request: Request,
    mcp: McpManager,
    name: str,
    arguments_json: str,
    session_key: str,
) -> str:
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        args = {}

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
                "x-freva-vault-url": request.headers.get("x-freva-vault-url", ""),
                "x-freva-rest-url":  request.headers.get("x-freva-rest-url", ""),
            },
        )
        return res.result if res.ok else f"ERROR: {res.error or res.raw}"

    if name == "code_interpreter":
        auth = request.headers.get("authorization")
        res = mcp.call_tool(
            "code",
            session_key=session_key,
            name="code_interpreter",
            arguments={"code": args.get("code", "")},
            extra_headers={"Authorization": auth} if auth else None,
        )
        return res.result if res.ok else f"ERROR: {res.error or res.raw}"

    return f"ERROR: unknown tool '{name}'"


# --------- public: stream with tool orchestration -----------------------------

async def stream_with_tools(
    request: Request,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    mcp: McpManager,
    acomplete_func,                            # pass src.services.models.litellm_client.acomplete
    tools: Optional[List[Dict[str, Any]]] = None,  # include if your model requires explicit tool schemas
) -> AsyncIterator[str]:
    """
    Yields assistant text chunks. If tool_calls appear, pauses stream, executes tools via MCP,
    then starts a second streamed completion with tool results appended.
    """
    # 1) first streamed call
    kwargs = {"model": model, "messages": messages, "stream": True}
    if tools is not None:
        kwargs["tools"] = tools
    resp = await acomplete_func(**kwargs)

    agg_for_tools: Dict[str, Any] = {}
    tool_calls_detected = False

    if hasattr(resp, "__aiter__"):
        async for chunk in resp:  # type: ignore
            _accumulate_tool_calls(chunk, agg_for_tools)
            tcs = _finalize_tool_calls(agg_for_tools)
            if tcs:
                tool_calls_detected = True
                break  # stop forwarding first stream; we’ll run tools
            # forward normal text
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece
            if choice.get("finish_reason"):
                break
    else:
        # non-streaming fallback
        from src.services.models.litellm_client import first_text
        full_txt = first_text(resp) or ""
        if full_txt:
            import re
            for piece in re.findall(r"\S+\s*", full_txt):
                yield piece

    if not tool_calls_detected:
        return  # nothing more to do

    # 2) run tools
    session_key = (
        request.headers.get("x-thread-id")
        or request.query_params.get("thread_id")
        or (request.client.host if request.client else "session")
    )
    tcs = _finalize_tool_calls(agg_for_tools)
    tool_result_messages: List[Dict[str, Any]] = []
    for i, tc in enumerate(tcs):
        name = (tc.get("function") or {}).get("name") or ""
        arguments = (tc.get("function") or {}).get("arguments") or ""
        content = await _run_tool_via_mcp(request, mcp, name, arguments, session_key)
        tool_result_messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id") or f"tc_{i}",
            "content": content if isinstance(content, str) else json.dumps(content),
        })

    # 3) second streamed call: original messages + assistant tool_calls + tool results
    assistant_msg_with_calls = {"role": "assistant", "tool_calls": tcs}
    second_messages = messages + [assistant_msg_with_calls] + tool_result_messages

    kwargs2 = {"model": model, "messages": second_messages, "stream": True}
    if tools is not None:
        kwargs2["tools"] = tools
    resp2 = await acomplete_func(**kwargs2)

    if hasattr(resp2, "__aiter__"):
        async for chunk in resp2:  # type: ignore
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                yield piece
            if choice.get("finish_reason"):
                break
    else:
        from src.services.models.litellm_client import first_text
        full_txt = first_text(resp2) or ""
        if full_txt:
            import re
            for piece in re.findall(r"\S+\s*", full_txt):
                yield piece
