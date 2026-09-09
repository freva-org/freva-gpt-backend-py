from __future__ import annotations

import json
from typing import Any, Dict, List, Union, cast

from typing_extensions import TypedDict

from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.settings import get_settings

from .stream_variants import (
    Conversation,
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVImage,
    SVOpenAIError,
    SVPrompt,
    SVServerError,
    SVServerHint,
    SVStreamEnd,
    SVToolCall,
    SVToolOutput,
    SVUser,
    normalize_conv_for_prompt,
)

logger = configure_logging(__name__)
settings = get_settings()

# Roles (OpenAI Chat)
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

# Conventions
TOOL_NAME_CODE = "code_interpreter"


class OpenAIMessage(TypedDict, total=False):
    role: str
    content: Any
    name: str
    tool_calls: list[dict]
    tool_call_id: str  # for tool role


def _as_system(content: Union[str, dict, list]) -> OpenAIMessage:
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    return {"role": ROLE_SYSTEM, "content": content}


def _tool_call_message(args: str, call_id: str, tool_name: str) -> OpenAIMessage:
    # Arguments should be a JSON string per OpenAI function-call schema.
    return {
        "role": ROLE_ASSISTANT,
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": args},
            }
        ],
    }


def _tool_result_message(output: str, call_id: str, tool_name: str) -> OpenAIMessage:
    return {
        "role": ROLE_TOOL,
        "name": tool_name,
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


def _image_user_url_message(url: str) -> OpenAIMessage:
    return {
        "role": ROLE_USER,
        "content": [
            {
                "type": "text",
                "text": "Here is the image returned by the Code Interpreter.",
            },
            {
                "type": "image_url",
                "image_url": {"url": url},
            },
        ],
    }


def mcp_tool_to_openai_function(tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an MCP tool descriptor to OpenAI-style tool schema:
    MCP (typical):
      {"name": "search", "description": "...", "input_schema": {...}}
    OpenAI tool:
      {"type":"function","function":{"name":"search","description":"...","parameters":{...}}}
    Be permissive: fall back to {} if schema missing.
    """
    name = tool.get("name") or ""
    desc = tool.get("description") or ""
    params = tool.get("input_schema") or tool.get("parameters") or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params if isinstance(params, dict) else {},
        },
    }


def _extend_with_prompt_json(out: List[OpenAIMessage], json_str: str) -> None:
    try:
        data = json.loads(json_str)
    except Exception as e:
        logger.warning(
            "Failed to parse Prompt JSON payload: %s; skipping this Prompt variant.", e
        )
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
        out.append(
            cast(OpenAIMessage, msg)
        )  # trust caller for deeper schema (tool_calls etc.)


def help_convert_sv_ccrm(
    conversation: Conversation,
    include_images: bool = False,
    include_meta: bool = False,
) -> List[OpenAIMessage]:
    """
    Convert a StreamVariant conversation to OpenAI ChatCompletion messages.
    • include_images: whether to include Image variants
    • include_meta: whether to include ServerHint/Errors/StreamEnd as system/tool messages
    """
    conv = normalize_conv_for_prompt(conversation, include_meta=include_meta)
    out: List[OpenAIMessage] = []

    for v in conv:
        if isinstance(v, SVPrompt):
            _extend_with_prompt_json(out, v.content)

        elif isinstance(v, SVUser):
            out.append({"role": ROLE_USER, "content": v.content})

        elif isinstance(v, SVAssistant):
            out.append({"role": ROLE_ASSISTANT, "content": v.content})

        elif isinstance(v, SVCode):
            out.append(_tool_call_message(v.content, v.id, tool_name=TOOL_NAME_CODE))

        elif isinstance(v, SVCodeOutput):
            image_msgs = []

            for i, file in enumerate(v.content.created_files):
                # Send the image-url to the model, only if it not already sent
                if not file.url_sent_to_model:
                    file_type = file.mime_type
                    if ("image" in file_type) and (not settings.DEV):
                        # In local dev, the image URL is "localhost:...". Since it is unreachable
                        # for the model, it causes LiteLLM 400 Bad Request.
                        # So we send the URL to the model only on production.
                        image_url = file.preview_url
                        if image_url is not None:
                            image_msgs.append(_image_user_url_message(url=image_url))
                            file.url_sent_to_model = True

            out.append(
                _tool_result_message(
                    v.content.llm_payload,
                    v.id,
                    tool_name=TOOL_NAME_CODE,
                )
            )
            out.extend(image_msgs)

        elif isinstance(v, SVToolCall):
            out.append(_tool_call_message(v.content, v.id, tool_name=v.tool_name))

        elif isinstance(v, SVToolOutput):
            out.append(
                _tool_result_message(
                    v.content.model_dump_json(), v.id, tool_name=v.tool_name
                )
            )

        elif isinstance(v, SVImage):
            if include_images:
                out.append(_image_user_message(v.content, v.mime))
            else:
                logger.debug("Dropping Image variant in prompt (include_images=False).")

        elif isinstance(v, SVServerHint):
            if include_meta:
                out.append(_as_system(v.content))

        elif isinstance(v, SVServerError):
            if include_meta:
                out.append(_as_system(v.content))

        elif isinstance(v, SVOpenAIError):
            if include_meta:
                out.append(_as_system(v.content))

        elif isinstance(v, SVStreamEnd):
            if include_meta:
                out.append(_as_system(v.content))

        else:
            logger.warning("Unknown StreamVariant encountered: %r", v)

    return out
