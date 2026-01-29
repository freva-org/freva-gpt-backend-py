from __future__ import annotations

import asyncio
import json

from typing import Any, Dict, List
from dataclasses import dataclass

from src.services.service_factory import McpManager
from src.core.logging_setup import configure_logging

from src.services.streaming.stream_variants import (
    SVUser,
    SVCodeOutput,
    SVImage,
    SVAssistant,
    SVServerError,
    SVToolOutput,
    StreamVariant,
    help_convert_sv_ccrm, _tool_result_message
)


DEFAULT_LOGGER = configure_logging(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# MCP tool runner
# ──────────────────────────────────────────────────────────────────────────────

async def run_tool_via_mcp(
    *,
    mcp: McpManager,
    tool_name: str,
    arguments_json: str,
    logger=None,
) -> str:
    log = logger or DEFAULT_LOGGER
    try:
        args = json.loads(arguments_json or "{}")
    except Exception:
        args = {"_raw": arguments_json}

    server_name = mcp.get_server_from_tool(tool_name)

    log.info(f"Executing tool call:\nname : {tool_name}   arguments : {args}")
    # Run the blocking MCP call in a thread so cancellation of the coroutine
    # doesn’t block the event loop.
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: mcp.call_tool(
            server_name,
            name=tool_name,
            arguments=args,
        ),
    )

    return json.dumps(res)


# ──────────────────────────────────────────────────────────────────────────────
# Tool-call accumulation helpers (OpenAI-style deltas)
# ──────────────────────────────────────────────────────────────────────────────

def accumulate_tool_calls(delta: Dict[str, Any], agg: Dict[str, Any]) -> None:
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

def finalize_tool_calls(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
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
# Tool result parsers
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class FinalSummary:
    var_block: list
    tool_messages: list
    is_error: bool


def parse_tool_result(out_txt: str, tool_name: str, call_id: str, logger=None):
    log = logger or DEFAULT_LOGGER
    if tool_name == "code_interpreter":
        yield from parse_code_interpreter_result(out_txt, call_id, logger=log)
    elif tool_name == "web_search":
        yield from parse_web_search_result(out_txt, call_id, logger=log)
    else:
        log.warning(f"Please implement output processing function for the tool {tool_name}")
        yield FinalSummary(var_block=[], tool_messages=[], is_error=True)


def parse_code_interpreter_result(result_txt: str, id: str, logger=None):
    log = logger or DEFAULT_LOGGER
    code_block : List[StreamVariant] = []
    code_msgs: List[Dict] = []

    # Code output: structured dict of displayed data, image or error   
    result_json = json.loads(result_txt)

    if "structuredContent" in result_json.keys():
        # Code output: structured dict of displayed data, image or error
        result = result_json.get("structuredContent")

        # Printed/displayed output + error message if exists
        out = "" + (("\n" + result["stdout"]) if result["stdout"] else "") + \
            (("\n" + result["result_repr"]) if result["result_repr"] else "") 
        out_error =(("\n" + result["stderr"]) if result["stderr"] else "") + \
            (("\n" + result["error"]) if result["error"] else "")
        if out or out_error:
            codeout = out + out_error
        else:
            codeout = "" # We must send something here, the model expects it.
        codeout_v = SVCodeOutput(output=codeout, id=id)
        yield codeout_v
        code_block.append(codeout_v)
        code_msgs.extend(help_convert_sv_ccrm([codeout_v]))

        # Image/html/json etc., rich output
        for i, r in enumerate(result.get("display_data", []) or []):
            if "image/png" in r.keys():
                base64_image = r["image/png"]
                image_id = id + f"_{i}"
                image_v = SVImage(b64=base64_image, id=image_id)
                yield image_v
                code_block.append(image_v)
                code_msgs.extend(help_convert_sv_ccrm([SVUser(text="Here is the image returned by the Code Interpreter."), image_v],
                                                      include_images=True))

            if "application/json" in r.keys():
                json_v = SVCodeOutput(output=r["application/json"], id=f"{id}:json")
                yield json_v
                code_block.append(json_v)
                code_msgs.extend(help_convert_sv_ccrm([json_v]))
        isError = True if out_error else False
    else:
        if result_json.get("error"):
            out = f"Code-Server: {result_json.get('error')}"
        else:
            out = result_json.get("content", {}).get("text", "Unknown code interpreter response.")
        codeout_v = SVCodeOutput(output=out, id=id)
        yield codeout_v
        code_block.append(codeout_v)
        code_msgs.extend(help_convert_sv_ccrm([codeout_v]))
        isError = True
    yield FinalSummary(var_block=code_block, 
                       tool_messages=code_msgs, 
                       is_error=isError)


def parse_web_search_result(result_txt: str, id: str, logger=None):
    log = logger or DEFAULT_LOGGER
    isError = False

    # Code output: structured dict of displayed data, image or error   
    result_json = json.loads(result_txt)

    if "structuredContent" in result_json.keys():
        # Code output: structured dict of displayed data, image or error
        result = result_json.get("structuredContent")
        web_sv = SVToolOutput(output=result.get("result"), tool_name="web-search", id=id)
    else:
        if result_json.get("error"):
            out = f"Web-Search-Server: {result_json.get('error')}"
        else:
            out = result_json.get("content", {}).get("text", "Unknown web-search response.")
        web_sv = SVServerError(output=out, id=id)
        yield web_sv
        isError = True
    web_msg = help_convert_sv_ccrm([web_sv])
    yield FinalSummary(var_block=[web_sv], 
                       tool_messages=web_msg, 
                       is_error=isError)