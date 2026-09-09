from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from climateclaw.core.logging_setup import configure_logging
from climateclaw.tools.models import CodeInterpreterResult, GenericToolResult

"""
• Class-based StreamVariant models (discriminator: `variant`)
• Conversation utilities: cleanup_conversation(), normalize_conv_for_prompt()
• Json <-> class conversion helpers: from_json_to_sv(), from_sv_to_json(), parse_examples_jsonl()

Notes
-----
• examples.jsonl is stored in wire shape; use parse_examples_jsonl(...) to read it as classes.
• Pydantic model fields intentionally match the wire/json shape:
    {"variant": "...", "content": ..., ...}
"""

logger = configure_logging(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants / Conventions
# ──────────────────────────────────────────────────────────────────────────────

# Variant names (runtime constants)
PROMPT = "Prompt"
USER = "User"
ASSISTANT = "Assistant"
CODE = "Code"
CODE_OUTPUT = "CodeOutput"
TOOL_CALL = "ToolCall"
TOOL_OUTPUT = "ToolOutput"
IMAGE = "Image"
SERVER_ERROR = "ServerError"
OPENAI_ERROR = "OpenAIError"
STREAM_END = "StreamEnd"
SERVER_HINT = "ServerHint"

# ──────────────────────────────────────────────────────────────────────────────
# StreamVariant classes (Pydantic v2, discriminated by `variant`)
# ──────────────────────────────────────────────────────────────────────────────


class _SVBase(BaseModel):
    """Base for all StreamVariants."""

    model_config = ConfigDict(frozen=True)  # make instances hashable/immutable


class SVPrompt(_SVBase):
    variant: Literal["Prompt"] = Field(default="Prompt")
    content: str = Field(..., description="JSON string of ChatCompletion messages")


class SVUser(_SVBase):
    variant: Literal["User"] = Field(default="User")
    content: str
    model: str = Field(
        default="", description="Model used to respond to this user request"
    )


class SVAssistant(_SVBase):
    variant: Literal["Assistant"] = Field(default="Assistant")
    content: str
    feedback: str = Field(default="", description="User feedback")


class SVCode(_SVBase):
    variant: Literal["Code"] = Field(default="Code")
    content: str
    id: str
    feedback: str = Field(default="", description="User feedback")


class SVCodeOutput(_SVBase):
    variant: Literal["CodeOutput"] = Field(default="CodeOutput")
    content: CodeInterpreterResult
    id: str


class SVImage(_SVBase):
    variant: Literal["Image"] = Field(default="Image")
    content: str
    id: str
    mime: str = Field(default="image/png")


class SVToolCall(_SVBase):
    variant: Literal["ToolCall"] = Field(default="ToolCall")
    content: str
    tool_name: str
    id: str


class SVToolOutput(_SVBase):
    variant: Literal["ToolOutput"] = Field(default="ToolOutput")
    content: GenericToolResult
    tool_name: str
    id: str


class SVServerHint(_SVBase):
    variant: Literal["ServerHint"] = Field(default="ServerHint")
    content: dict | str


class SVServerError(_SVBase):
    variant: Literal["ServerError"] = Field(default="ServerError")
    content: str


class SVOpenAIError(_SVBase):
    variant: Literal["OpenAIError"] = Field(default="OpenAIError")
    content: str


class SVStreamEnd(_SVBase):
    variant: Literal["StreamEnd"] = Field(default="StreamEnd")
    content: str


# Discriminated union type for parsing
StreamVariant = Annotated[
    SVPrompt
    | SVUser
    | SVAssistant
    | SVCode
    | SVCodeOutput
    | SVToolCall
    | SVToolOutput
    | SVImage
    | SVServerHint
    | SVServerError
    | SVOpenAIError
    | SVStreamEnd,
    Field(discriminator="variant"),
]

Conversation = list[StreamVariant]

SVDict = dict[
    str, str | list[str] | dict[str, Any]
]  # for when handling variants as dicts (e.g. from JSON)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: conversation normalization
# ──────────────────────────────────────────────────────────────────────────────


def cleanup_conversation(
    conv: Conversation,
    append_stream_end: bool = False,
) -> Conversation:
    """
    Insert missing CodeOutput after Code and optionally ensure StreamEnd at the end.
    """
    out: Conversation = []
    pending_code_id: str | None = None

    for v in conv:
        # If there is a pending Code (no output yet) and the next item is not CodeOutput,
        # insert an empty CodeOutput before appending the new item.
        if pending_code_id is not None and not isinstance(v, SVCodeOutput):
            out.append(
                SVCodeOutput(
                    content=create_code_interpreter_output(
                        error="No response was received from code-interpreter."
                    ),
                    id=pending_code_id,
                )
            )
            pending_code_id = None

        if isinstance(v, SVCode):
            pending_code_id = v.id

        elif isinstance(v, SVCodeOutput):
            if pending_code_id is not None and v.id != pending_code_id:
                logger.warning(
                    "CodeOutput.id=%s does not match pending Code.id=%s.",
                    v.id,
                    pending_code_id,
                )
            pending_code_id = None

        out.append(v)

    if pending_code_id is not None:
        # close dangling code with an empty output
        out.append(
            SVCodeOutput(
                content=create_code_interpreter_output(
                    error="No response was received from code-interpreter."
                ),
                id=pending_code_id,
            )
        )

    # Ensure ends with StreamEnd (only if requested)
    if append_stream_end:
        if not out or not isinstance(out[-1], SVStreamEnd):
            out.append(SVStreamEnd(content="Stream ended in a very unexpected manner"))

    return out


def normalize_conv_for_prompt(
    conv: Conversation, include_meta: bool = True
) -> Conversation:
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
        if isinstance(v, (SVServerHint, SVServerError, SVOpenAIError, SVStreamEnd)):
            # Drop meta if include_meta=False (Rust-like behavior)
            continue
        filtered.append(v)

    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# JSON <-> Stream Variant class conversion + examples loader
# ──────────────────────────────────────────────────────────────────────────────


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_code_content(
    content: Any,
    id_from_obj: Any,
) -> tuple[str, str]:
    """
    Supports:
        {"variant": "Code", "content": "...", "id": "..."}
        {"variant": "Code", "content": ["...", "..."]}
        {"variant": "Code", "content": [{"code": "..."}, "..."]}
    """
    if isinstance(content, list) and len(content) >= 2:
        # Legacy {"variant":"Code","content":["{\"code\":\"...\"}", "call_ABC"]}
        payload, call_id = content[0], content[1]
        return str(payload), call_id

    return _as_str(content), _as_str(id_from_obj)


def _parse_code_output_content(
    content: Any,
    id_from_obj: Any,
) -> tuple[Any, str]:
    """
    Supports:
        {"variant": "CodeOutput", "content": {...}, "id": "..."}
        {"variant": "CodeOutput", "content": "...", "id": "..."}
        {"variant": "CodeOutput", "content": ["...", "..."]}
    """
    if isinstance(content, list) and len(content) >= 2:
        # Legacy {"variant":"CodeOutput","content":["<repr>", "call_ABC"]}
        return content[0], _as_str(content[1])

    return content, _as_str(id_from_obj)


def from_json_to_sv(obj: dict) -> StreamVariant:
    """
    Convert a json/dict into class-based StreamVariant.

    This is the compatibility boundary. It accepts both current wire shape and
    older examples.jsonl shapes, then returns normalized Pydantic variants whose
    fields match the wire shape.
    """
    v = obj.get("variant")
    c = obj.get("content", "")
    f = obj.get("feedback", "")

    if v == ASSISTANT:
        return SVAssistant(content=_as_str(c), feedback=f)
    if v == USER:
        m = obj.get("model")
        return SVUser(
            content=_as_str(c),
            model=_as_str(m),
        )
    if v == PROMPT:
        return SVPrompt(content=_as_str(c))
    if v == SERVER_HINT:
        return SVServerHint(content=c if isinstance(c, dict) else json.loads(c))
    if v == SERVER_ERROR:
        return SVServerError(content=_as_str(c))
    if v == OPENAI_ERROR:
        return SVOpenAIError(content=_as_str(c))
    if v == STREAM_END:
        return SVStreamEnd(content=_as_str(c))
    if v == IMAGE:
        return SVImage(content=_as_str(c), id=_as_str(obj.get("id")))

    if v == CODE:
        code_content, call_id = _parse_code_content(c, obj.get("id", ""))
        return SVCode(content=code_content, id=call_id, feedback=f)

    if v == CODE_OUTPUT:
        output, call_id = _parse_code_output_content(c, obj.get("id", ""))
        return SVCodeOutput(content=normalize_code_output(output), id=_as_str(call_id))

    if v == TOOL_CALL:
        return SVToolCall(
            content=_as_str(c),
            id=_as_str(obj.get("id")),
            tool_name=_as_str(obj.get("tool_name")),
        )

    if v == TOOL_OUTPUT or v == TOOL_CALL:
        return SVToolOutput(
            content=normalize_generic_tool_output(c),
            id=_as_str(obj.get("id")),
            tool_name=_as_str(obj.get("tool_name")),
        )

    raise ValueError(f"unsupported variant: {obj!r}")


def from_sv_to_json(v: StreamVariant) -> SVDict:
    """
    Convert Pydantic StreamVariant back to json/dict.
    """
    return v.model_dump(exclude_none=True)


def parse_examples_jsonl(path: str | Path) -> list[StreamVariant]:
    """
    Read examples.jsonl (JSON lines), tolerate noise, return class-based variants.
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
                out.append(from_json_to_sv(obj))
            except Exception:
                # skip unparseable lines
                continue

    return out


# ──────────────────────────────────────────────────────────────────────────────
# Code interpreter output shape
# ──────────────────────────────────────────────────────────────────────────────


def create_code_interpreter_output(
    stdout: str = "",
    stderr: str = "",
    result_repr: str = "",
    display_data: list = [],
    error: str = "",
    created_files: list = [],
) -> CodeInterpreterResult:
    return CodeInterpreterResult(
        stdout=stdout,
        stderr=stderr,
        result_repr=result_repr,
        display_data=display_data,
        error=error,
        created_files=created_files,
    )


def _normalize_display_data(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []

    if isinstance(value, list):
        return [
            (
                {k: v for k, v in item.items() if k != "image/png"}
                if isinstance(item, dict)
                else {"text/plain": str(item)}
            )
            for item in value
        ]

    return [{"text/plain": str(value)}]


def normalize_code_output(out: Any) -> CodeInterpreterResult:
    """
    Normalize current and legacy CodeOutput payloads into the actual
    code_interpreter output shape:

    Accepted inputs:
    - current code_interpreter dict
    - legacy flattened string
    - legacy list forms
    - None
    """
    if out is None:
        return create_code_interpreter_output()

    if isinstance(out, CodeInterpreterResult):
        norm_model = out.model_copy(deep=True)
        norm_model.display_data = _normalize_display_data(norm_model.display_data)
        return norm_model

    if isinstance(out, dict):
        norm_dict: dict[str, Any] = {
            **out,
            "display_data": _normalize_display_data(out.get("display_data")),
        }
        return CodeInterpreterResult.model_validate(norm_dict)

    if isinstance(out, list):
        text = out[0]
    else:
        try:
            out_json = json.loads(out)
            norm_json: dict[str, Any] = {
                **out_json,
                "display_data": _normalize_display_data(out_json.get("display_data")),
            }
            return CodeInterpreterResult.model_validate(norm_json)
        except (TypeError, json.JSONDecodeError):
            text = str(out)

    return create_code_interpreter_output(stdout=text)


# ──────────────────────────────────────────────────────────────────────────────
# Generic tool output shape
# ──────────────────────────────────────────────────────────────────────────────


def normalize_generic_tool_output(out: Any) -> GenericToolResult:
    """
    Normalize current and legacy ToolOutput payloads into the actual
    tool output shape:

    Accepted inputs:
    - current Pydantic model or dict
    - legacy string
    - None
    """
    if out is None:
        return GenericToolResult()

    if isinstance(out, GenericToolResult):
        return out

    if isinstance(out, dict):
        out = GenericToolResult.model_validate(out)
        return out

    return GenericToolResult(result=out)


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

    # Dict-shaped
    if isinstance(variant, dict):
        name = variant.get("variant") or variant.get("type") or variant.get("kind")
        if isinstance(name, str) and name.strip().lower() == "prompt":
            return True
        return False

    # Object with attributes
    name = (
        getattr(variant, "variant", None)
        or getattr(variant, "type", None)
        or getattr(variant, "kind", None)
    )
    if isinstance(name, str) and name.strip().lower() == "prompt":
        return True

    # Fallback to class name
    cls = variant.__class__.__name__ if variant is not None else ""
    return cls.lower() in ("prompt", "svprompt")


__all__ = [
    # Classes / types
    "StreamVariant",
    "Conversation",
    "SVPrompt",
    "SVUser",
    "SVAssistant",
    "SVCode",
    "SVCodeOutput",
    "SVImage",
    "SVServerHint",
    "SVServerError",
    "SVOpenAIError",
    "SVStreamEnd",
    # Constants / roles
    "PROMPT",
    "USER",
    "ASSISTANT",
    "CODE",
    "CODE_OUTPUT",
    "IMAGE",
    "SERVER_ERROR",
    "OPENAI_ERROR",
    "STREAM_END",
    "SERVER_HINT",
    # Functions
    "cleanup_conversation",
    "normalize_conv_for_prompt",
    "is_prompt",
    "from_json_to_sv",
    "from_sv_to_json",
    "parse_examples_jsonl",
]
