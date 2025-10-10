from __future__ import annotations
"""
Mirror of Rust enum `StreamVariant` and alias `Conversation = Vec<StreamVariant>`,
with Pythonic refactor to typed classes (Pydantic v2 discriminated union).

What this module provides
-------------------------
• Class-based StreamVariant models (discriminator: `variant`)
• Conversation utilities: cleanup_conversation(), normalize_for_prompt()
• Conversion to OpenAI Chat messages: help_convert_sv_ccrm(...)
• Wire <-> class conversion helpers: from_wire_dict(), to_wire_dict(), parse_examples_jsonl()

Differences vs Rust (documented for future parity work)
-------------------------------------------------------
1) SVPrompt field name:
   - Wire uses: {"variant":"Prompt","content":"<json string>"}
   - Class uses: SVPrompt.payload (renamed from 'json' to avoid BaseModel.json() clash)
2) Images:
   - Wire expected: {"variant":"Image","content":{"b64":"...","mime":"image/png"}}
   - Class uses: SVImage(b64, mime). We also tolerate legacy {"url": "..."} in from_wire_dict (drops to blank b64).
3) Tool-call mapping for Code/CodeOutput:
   - Code → assistant tool_call ("code_interpreter") with args {"code": "<code>"}.
   - CodeOutput → tool message with tool_call_id and name "code_interpreter".
4) Meta inclusion toggle:
   - help_convert_sv_ccrm(..., include_meta=False) ≈ Rust prompting behavior (drops meta).
   - Default is include_meta=True per our phase plan.

Notes
-----
• examples.jsonl is stored in wire shape; use parse_examples_jsonl(...) to read it as classes.
• Assistant name convention matches Rust tests: "frevaGPT".
"""

from typing import Annotated, Literal, Optional, Union, List, Dict, Any
from typing_extensions import TypedDict
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants / Conventions
# ──────────────────────────────────────────────────────────────────────────────

# Variant names (runtime constants; do NOT use these inside Literal[...] types)
PROMPT = "Prompt"
USER = "User"
ASSISTANT = "Assistant"
CODE = "Code"
CODE_OUTPUT = "CodeOutput"
IMAGE = "Image"
SERVER_ERROR = "ServerError"
OPENAI_ERROR = "OpenAIError"
CODE_ERROR = "CodeError"
STREAM_END = "StreamEnd"
SERVER_HINT = "ServerHint"

# Roles (OpenAI Chat)
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

# Conventions
ASSISTANT_NAME = "frevaGPT"
TOOL_NAME_CODE = "code_interpreter"


# ──────────────────────────────────────────────────────────────────────────────
# StreamVariant classes (Pydantic v2, discriminated by `variant`)
# ──────────────────────────────────────────────────────────────────────────────

class _SVBase(BaseModel):
    """Base for all StreamVariants."""
    model_config = ConfigDict(frozen=True)  # make instances hashable/immutable


class SVPrompt(_SVBase):
    # IMPORTANT: use string literals inside Literal[...] to satisfy type-checkers
    variant: Literal["Prompt"] = Field(default=PROMPT)
    # JSON string representing a list of chat messages (OpenAI format).
    payload: str = Field(..., description="JSON string of ChatCompletion messages")


class SVUser(_SVBase):
    variant: Literal["User"] = Field(default=USER)
    text: str


class SVAssistant(_SVBase):
    variant: Literal["Assistant"] = Field(default=ASSISTANT)
    text: str
    name: str = Field(default=ASSISTANT_NAME, description="Assistant display name")


class SVCode(_SVBase):
    variant: Literal["Code"] = Field(default=CODE)
    code: str
    call_id: str


class SVCodeOutput(_SVBase):
    variant: Literal["CodeOutput"] = Field(default=CODE_OUTPUT)
    output: str
    call_id: str


class SVImage(_SVBase):
    variant: Literal["Image"] = Field(default=IMAGE)
    b64: str
    mime: str = Field(default="image/png")


class SVServerHint(_SVBase):
    variant: Literal["ServerHint"] = Field(default=SERVER_HINT)
    data: Union[dict, str]


class SVServerError(_SVBase):
    variant: Literal["ServerError"] = Field(default=SERVER_ERROR)
    message: str


class SVOpenAIError(_SVBase):
    variant: Literal["OpenAIError"] = Field(default=OPENAI_ERROR)
    message: str


class SVCodeError(_SVBase):
    variant: Literal["CodeError"] = Field(default=CODE_ERROR)
    message: str
    call_id: Optional[str] = None


class SVStreamEnd(_SVBase):
    variant: Literal["StreamEnd"] = Field(default=STREAM_END)
    message: str


# Discriminated union type for parsing
StreamVariant = Annotated[
    Union[
        SVPrompt,
        SVUser,
        SVAssistant,
        SVCode,
        SVCodeOutput,
        SVImage,
        SVServerHint,
        SVServerError,
        SVOpenAIError,
        SVCodeError,
        SVStreamEnd,
    ],
    Field(discriminator="variant"),
]

Conversation = List[StreamVariant]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: conversation normalization
# ──────────────────────────────────────────────────────────────────────────────

def cleanup_conversation(conv: Conversation, append_stream_end: bool = True) -> Conversation:
    """
    Insert missing CodeOutput after Code and ensure StreamEnd at the end.
    Mirrors the spirit of Rust's cleanup_conversation with class-based variants.
    """
    out: Conversation = []
    pending_code_id: Optional[str] = None

    for v in conv:
        # If there is a pending Code (no output yet) and the next item is not CodeOutput,
        # insert an empty CodeOutput before appending the new item.
        if pending_code_id is not None and not isinstance(v, SVCodeOutput):
            out.append(SVCodeOutput(output="", call_id=pending_code_id))
            pending_code_id = None

        if isinstance(v, SVCode):
            pending_code_id = v.call_id
        elif isinstance(v, SVCodeOutput):
            if pending_code_id is not None and v.call_id != pending_code_id:
                logger.warning(
                    "CodeOutput.call_id=%s does not match pending Code.call_id=%s.",
                    v.call_id, pending_code_id
                )
            pending_code_id = None

        out.append(v)

    if pending_code_id is not None:
        # close dangling code with an empty output
        out.append(SVCodeOutput(output="", call_id=pending_code_id))

    # Ensure ends with StreamEnd (only if requested)
    if append_stream_end:
        if not out or not isinstance(out[-1], SVStreamEnd):
            out.append(SVStreamEnd(message="Stream ended in a very unexpected manner"))

    return out


def normalize_for_prompt(conv: Conversation, include_meta: bool = True) -> Conversation:
    """
    Prepare a conversation for conversion into chat messages.
    - Applies cleanup_conversation
    - Optionally filters out meta variants if include_meta=False
    """
    conv = cleanup_conversation(conv)
    if include_meta:
        return conv

    filtered: Conversation = []
    for v in conv:
        if isinstance(v, (SVServerHint, SVServerError, SVOpenAIError, SVCodeError, SVStreamEnd)):
            # Drop meta if include_meta=False (Rust-like behavior)
            continue
        filtered.append(v)
    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# Conversion to OpenAI Chat messages
# ──────────────────────────────────────────────────────────────────────────────

class OpenAIMessage(TypedDict, total=False):
    role: str
    content: Any
    name: str
    tool_calls: list[dict]
    tool_call_id: str  # for tool role


def _as_system(name: str, content: Union[str, dict, list]) -> OpenAIMessage:
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    return {"role": ROLE_SYSTEM, "name": name, "content": content}


def _code_tool_call_message(code: str, call_id: str) -> OpenAIMessage:
    # Arguments should be a JSON string per OpenAI function-call schema.
    arguments = json.dumps({"code": code}, ensure_ascii=False)
    return {
        "role": ROLE_ASSISTANT,
        "name": ASSISTANT_NAME,
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": TOOL_NAME_CODE, "arguments": arguments},
            }
        ],
    }


def _code_tool_result_message(output: str, call_id: str) -> OpenAIMessage:
    return {
        "role": ROLE_TOOL,
        "name": TOOL_NAME_CODE,
        "tool_call_id": call_id,
        "content": output,
    }


def _image_user_message(b64: str, mime: str) -> OpenAIMessage:
    return {
        "role": ROLE_USER,
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        ],
    }


def _extend_with_prompt_json(out: List[OpenAIMessage], json_str: str) -> None:
    try:
        data = json.loads(json_str)
    except Exception as e:
        logger.warning("Failed to parse Prompt JSON payload: %s; skipping this Prompt variant.", e)
        return

    if not isinstance(data, list):
        logger.warning("Prompt payload is not a list; skipping.")
        return

    for i, msg in enumerate(data):
        if not isinstance(msg, dict):
            logger.warning("Prompt message[%d] is not an object; skipping.", i)
            continue
        role = msg.get("role")
        if role not in (ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL):
            logger.warning("Prompt message[%d] has invalid role=%r; skipping.", i, role)
            continue
        out.append(msg)  # trust caller for deeper schema (tool_calls etc.)


def help_convert_sv_ccrm(
    conversation: Conversation,
    include_images: bool = False,
    include_meta: bool = True,
) -> List[OpenAIMessage]:
    """
    Convert a StreamVariant conversation to OpenAI ChatCompletion messages.
    • include_images: whether to include Image variants (Rust passes false for prompting)
    • include_meta: whether to include ServerHint/Errors/StreamEnd as system/tool messages
      (Rust generally excludes; we default to True per our plan)
    """
    conv = normalize_for_prompt(conversation, include_meta=include_meta)
    out: List[OpenAIMessage] = []

    for v in conv:
        if isinstance(v, SVPrompt):
            _extend_with_prompt_json(out, v.payload)

        elif isinstance(v, SVUser):
            out.append({"role": ROLE_USER, "content": v.text})

        elif isinstance(v, SVAssistant):
            out.append({"role": ROLE_ASSISTANT, "name": v.name, "content": v.text})

        elif isinstance(v, SVCode):
            out.append(_code_tool_call_message(v.code, v.call_id))

        elif isinstance(v, SVCodeOutput):
            out.append(_code_tool_result_message(v.output, v.call_id))

        elif isinstance(v, SVImage):
            if include_images:
                out.append(_image_user_message(v.b64, v.mime))
            else:
                logger.debug("Dropping Image variant in prompt (include_images=False).")

        elif isinstance(v, SVServerHint):
            if include_meta:
                out.append(_as_system("server_hint", v.data))

        elif isinstance(v, SVServerError):
            if include_meta:
                out.append(_as_system("server_error", v.message))

        elif isinstance(v, SVOpenAIError):
            if include_meta:
                out.append(_as_system("openai_error", v.message))

        elif isinstance(v, SVCodeError):
            if include_meta and v.call_id:
                out.append(_code_tool_result_message(v.message, v.call_id))
            elif include_meta:
                out.append(_as_system("code_error", v.message))

        elif isinstance(v, SVStreamEnd):
            if include_meta:
                out.append(_as_system("stream_end", v.message))

        else:
            logger.warning("Unknown StreamVariant encountered: %r", v)

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Wire <-> class conversion + examples loader
# ──────────────────────────────────────────────────────────────────────────────

def from_wire_dict(obj: dict) -> StreamVariant:
    """
    Convert a legacy/wire-shaped dict into our class-based StreamVariant.
    Wire shape examples:
      {"variant":"Assistant","content":"..."}
      {"variant":"User","content":"..."}
      {"variant":"Code","content":["{\"code\":\"...\"}", "call_ABC"]}
      {"variant":"CodeOutput","content":["<repr>", "call_ABC"]}
      {"variant":"Image","content":{"b64":"...","mime":"image/png"}}
    """
    v = obj.get("variant")
    c = obj.get("content")

    if v == ASSISTANT:
        return SVAssistant(text="" if c is None else str(c))
    if v == USER:
        return SVUser(text="" if c is None else str(c))
    if v == PROMPT:
        return SVPrompt(payload="" if c is None else str(c))
    if v == SERVER_HINT:
        return SVServerHint(data=c if isinstance(c, dict) else {})
    if v == SERVER_ERROR:
        return SVServerError(message="" if c is None else str(c))
    if v == OPENAI_ERROR:
        return SVOpenAIError(message="" if c is None else str(c))
    if v == STREAM_END:
        return SVStreamEnd(message="" if c is None else str(c))
    if v == IMAGE:
        if isinstance(c, dict):
            b64 = c.get("b64") or ""
            mime = c.get("mime") or "image/png"
            # tolerate {"url": "..."} legacy by mapping to empty b64
            return SVImage(b64=b64, mime=mime)
        elif isinstance(c, str):
            # legacy string URL; map to empty b64
            return SVImage(b64="", mime="image/png")

    if v == CODE:
        code_text, call_id = "", ""
        if isinstance(c, list) and len(c) >= 2:
            payload, call_id = c[0], c[1]
            if isinstance(payload, dict):
                code_text = payload.get("code") or payload.get("python") or payload.get("text") or ""
            elif isinstance(payload, str):
                try:
                    d = json.loads(payload)
                    code_text = d.get("code") or d.get("python") or d.get("text") or payload
                except Exception:
                    code_text = payload
            else:
                code_text = str(payload)
            return SVCode(code=code_text, call_id=str(call_id))

    if v == CODE_OUTPUT:
        if isinstance(c, list) and len(c) >= 2:
            output, call_id = c[0], c[1]
            return SVCodeOutput(output=str(output), call_id=str(call_id))

    raise ValueError(f"unsupported wire variant: {obj!r}")


def to_wire_dict(v: StreamVariant) -> dict:
    """
    Convert our Pydantic class back to the wire dict (serde-compatible) used by Rust.
    """
    d = v.model_dump()
    kind = d["variant"]
    if kind == USER:
        return {"variant": USER, "content": d["text"]}
    if kind == ASSISTANT:
        return {"variant": ASSISTANT, "content": d["text"]}
    if kind == PROMPT:
        return {"variant": PROMPT, "content": d["payload"]}
    if kind == SERVER_HINT:
        return {"variant": SERVER_HINT, "content": d["data"]}
    if kind == SERVER_ERROR:
        return {"variant": SERVER_ERROR, "content": d["message"]}
    if kind == OPENAI_ERROR:
        return {"variant": OPENAI_ERROR, "content": d["message"]}
    if kind == STREAM_END:
        return {"variant": STREAM_END, "content": d["message"]}
    if kind == IMAGE:
        return {"variant": IMAGE, "content": {"b64": d["b64"], "mime": d["mime"]}}
    if kind == CODE:
        return {"variant": CODE, "content": [{"code": d["code"]}, d["call_id"]]}
    if kind == CODE_OUTPUT:
        return {"variant": CODE_OUTPUT, "content": [d["output"], d["call_id"]]}
    return d


def parse_examples_jsonl(path: str | Path) -> list[StreamVariant]:
    """
    Read examples.jsonl (wire/legacy shape), tolerate noise, return class-based variants.
    """
    out: list[StreamVariant] = []
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            # keep quiet but skip — examples may include comments / non-json lines
            continue
        if isinstance(obj, dict) and "variant" in obj:
            try:
                out.append(from_wire_dict(obj))
            except Exception:
                # skip unparseable lines
                continue
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Minor utility from earlier dict-based API (kept for convenience)
# ──────────────────────────────────────────────────────────────────────────────

def is_prompt(variant: Any) -> bool:
    """
    Return True if a variant represents a Prompt.

    Accepts:
    - Class instances (SVPrompt or with attribute .variant/.type/.kind)
    - Dict-shaped wire payloads ({"variant": "Prompt", ...})
    - Fallback to class name ("Prompt"/"SVPrompt")
    """
    # Fast path for our Pydantic class
    if isinstance(variant, SVPrompt):
        return True

    # Dict-shaped (wire/legacy)
    if isinstance(variant, dict):
        name = variant.get("variant") or variant.get("type") or variant.get("kind")
        if isinstance(name, str) and name.strip().lower() == "prompt":
            return True
        return False

    # Object with attributes
    name = getattr(variant, "variant", None) or getattr(variant, "type", None) or getattr(variant, "kind", None)
    if isinstance(name, str) and name.strip().lower() == "prompt":
        return True

    # Fallback to class name
    cls = variant.__class__.__name__ if variant is not None else ""
    return cls.lower() in ("prompt", "svprompt")



__all__ = [
    # Classes / types
    "StreamVariant", "Conversation",
    "SVPrompt", "SVUser", "SVAssistant", "SVCode", "SVCodeOutput",
    "SVImage", "SVServerHint", "SVServerError", "SVOpenAIError",
    "SVCodeError", "SVStreamEnd",
    # Constants / roles
    "PROMPT", "USER", "ASSISTANT", "CODE", "CODE_OUTPUT", "IMAGE",
    "SERVER_ERROR", "OPENAI_ERROR", "CODE_ERROR", "STREAM_END", "SERVER_HINT",
    "ROLE_SYSTEM", "ROLE_USER", "ROLE_ASSISTANT", "ROLE_TOOL",
    "ASSISTANT_NAME", "TOOL_NAME_CODE",
    # Functions
    "cleanup_conversation", "normalize_for_prompt",
    "help_convert_sv_ccrm", "is_prompt",
    "from_wire_dict", "to_wire_dict", "parse_examples_jsonl",
]
