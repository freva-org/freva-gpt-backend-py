import re
import string
import random
import json
import logging

from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Dict, List, Optional
from ansi2html import Ansi2HTMLConverter

from src.core.logging_setup import configure_logging
from src.services.streaming.stream_variants import (
    SVCode,
    SVCodeOutput,
    SVImage,
    StreamVariant,
)

log = logging.getLogger(__name__)
configure_logging()


# TODO: talk to Bianca: sending html messages instead of stripping color codes
conv = Ansi2HTMLConverter(inline=True) # Jupyter sends the stdout or stderr as a string containing ANSI escape sequences (color codes). We parse them as html messages

# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def new_conversation_id(length: int = 32) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

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


def parse_tool_result(out_txt: str, tool_name: str, call_id: str):
    if tool_name == "code_interpreter":
        for r in code_interpreter_aftermath(out_txt, call_id):
            yield r
    else:
        log.warning(f"Please implement output processing function for the tool {tool_name}")
        return [], [], False

# ──────────────────────────────────────────────────────────────────────────────
# Code-interpreter helpers
# ──────────────────────────────────────────────────────────────────────────────

def code_interpreter_aftermath(result_txt: str, id: str):
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
            comb_out = out + out_error
            codeout = strip_ansi(comb_out)
            codeout_v = SVCodeOutput(output=codeout, call_id=id)
            yield codeout_v
            code_block.append(codeout_v)
        else:
            codeout = "" # We must send something here, the model expects it. TODO Talk to Bianca about removing empty black box
        code_msgs.append(
            {"role": "tool", "tool_call_id": id, "name": "code_interpreter", "content": codeout}  
        )
            
        # Image/html/json etc., rich output
        for r in result.get("display_data", []) or []:
            if "image/png" in r.keys():
                base64_image = r["image/png"]
                image_v = SVImage(b64=base64_image)
                yield image_v
                code_block.append(image_v)
                code_msgs.append(
                    {"role": "user",  
                    "content": [{ "type": "text", "text": "Here is the image returned by the Code Interpreter." },
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}",}}
                                ]
                                })

            if "application/json" in r.keys():
                json_v = SVCodeOutput(output=r["application/json"], call_id=f"{id}:json")
                yield json_v
                code_block.append(json_v)
                code_msgs.append(
                    {"role": "tool", "tool_call_id": id, "name": "code_interpreter", "content": r["application/json"]}
                    )
        isError = True if out_error else False
    else:
        out = result_json.get("content", {}).get("text", "Unknown code interpreter response.")
        codeout_v = SVCodeOutput(output=out, call_id=id)
        yield codeout_v
        code_block.append(codeout_v)
        code_msgs.append(
            {"role": "tool", "tool_call_id": id, "name": "code_interpreter", "content": out}
            )
        isError = True
    yield FinalSummary(var_block=code_block, 
                       tool_messages=code_msgs, 
                       is_error=isError)
