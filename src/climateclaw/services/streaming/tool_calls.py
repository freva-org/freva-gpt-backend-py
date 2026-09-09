from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from climateclaw.core.logging_setup import configure_logging
from climateclaw.services.service_factory import McpManager
from climateclaw.services.streaming.openai_helpers import (
    OpenAIMessage,
    help_convert_sv_ccrm,
)
from climateclaw.services.streaming.stream_variants import (
    StreamVariant,
    SVCodeOutput,
    SVImage,
    SVToolOutput,
    SVUser,
    normalize_code_output,
)
from climateclaw.tools.models import (
    CodeInterpreterResult,
    GenericToolResult,
)

DEFAULT_LOGGER = configure_logging(__name__)
PROJECT_WEBSITE = os.environ.get("CLIMATECLAW_PROJECT_WEBSITE", "http://localhost:8000")


class InvalidToolArguments(ValueError):
    """Raised when tool arguments do not conform to the tool's JSON schema."""


@dataclass(frozen=True)
class NormalizedToolArguments:
    arguments: dict[str, Any]
    was_unwrapped: bool = False
    wrapper_key: str | None = None


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

    server_name = await mcp.get_server_from_tool(tool_name)
    if server_name is None:
        log.error(f"No MCP server found for tool={tool_name}")
        raise RuntimeError(f"No MCP server found for tool={tool_name}")

    log.info(f"Executing tool call:\nname : {tool_name}   arguments : {args}")
    res = await mcp.call_tool(
        server_name,
        name=tool_name,
        arguments=args,
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

    store: Dict[int, Dict[str, Any]] = agg.setdefault("by_index", {})
    for item in tc_list:
        idx = item.get("index")
        if idx is None:
            continue
        entry = store.setdefault(
            idx, {"type": "function", "function": {"name": "", "arguments": ""}}
        )
        if item.get("id"):
            entry["id"] = item["id"]
        f = item.get("function") or {}
        if f.get("name"):
            entry["function"]["name"] = f["name"]
        if f.get("arguments"):
            entry["function"]["arguments"] = (
                entry["function"].get("arguments", "") + f["arguments"]
            )


def finalize_tool_calls(agg: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = agg.get("by_index") or {}
    out: List[Dict[str, Any]] = []
    for idx in sorted(store.keys()):
        tc = store[idx]
        fn = tc.get("function") or {}
        tc.setdefault("type", "function")
        tc["function"] = {
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
        }
        out.append(tc)
    return out


def normalize_tool_arguments(
    raw_arguments: str,
    input_schema: dict[str, Any],
) -> NormalizedToolArguments:
    """
    Validate model-generated tool arguments.

    If the direct arguments do not satisfy the schema, allow exactly one
    arbitrary wrapper around an object that does satisfy the schema.

    Examples:

        {"code": "..."}
        -> accepted unchanged

        {"args": {"code": "..."}, "tool": "..."}
        -> normalized if {"code": "..."} satisfies that tool's schema

        {"arguments": "...", "code": "..."}
        -> rejected
    """
    arguments = _parse_tool_arguments(raw_arguments)

    if not input_schema:
        raise InvalidToolArguments("No input schema is available for this tool.")

    validator = Draft202012Validator(dict(input_schema))

    # First prefer the arguments exactly as generated.
    direct_error = _first_validation_error(validator, arguments)
    if direct_error is None:
        return NormalizedToolArguments(arguments=arguments)

    # The direct object is invalid. Test every immediate child object.
    valid_wrapped_arguments: list[tuple[str, dict[str, Any]]] = []

    for wrapper_key, wrapped_value in arguments.items():
        if not isinstance(wrapped_value, dict):
            continue

        wrapped_error = _first_validation_error(
            validator,
            wrapped_value,
        )

        if wrapped_error is None:
            valid_wrapped_arguments.append((wrapper_key, wrapped_value))

    # If exactly one child matches, normalization is unambiguous.
    if len(valid_wrapped_arguments) == 1:
        wrapper_key, wrapped_value = valid_wrapped_arguments[0]

        return NormalizedToolArguments(
            arguments=wrapped_value,
            was_unwrapped=True,
            wrapper_key=wrapper_key,
        )

    # More than one matching child would make choosing one unsafe, so reject
    if len(valid_wrapped_arguments) > 1:
        wrapper_keys = [wrapper_key for wrapper_key, _ in valid_wrapped_arguments]

        raise InvalidToolArguments(
            "Tool arguments contain multiple one-level objects that match "
            f"the declared input schema: {wrapper_keys!r}. "
            "The intended arguments are ambiguous."
        )

    raise InvalidToolArguments(
        "Tool arguments do not match the declared input schema. "
        f"Received: {arguments!r}. "
        f"Validation error: {_format_validation_error(direct_error)}"
    )


def _parse_tool_arguments(
    raw_arguments: str,
) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise InvalidToolArguments(
                f"Tool arguments are not a valid JSON string: {exc}"
            ) from exc
    else:
        raise InvalidToolArguments(
            f"Tool arguments must be a JSON string, not {type(raw_arguments).__name__}."
        )

    if not isinstance(parsed, dict):
        raise InvalidToolArguments(
            f"Tool arguments must decode to a JSON object, not {type(parsed).__name__}."
        )

    return parsed


def _first_validation_error(
    validator: Draft202012Validator,
    arguments: dict[str, Any],
) -> ValidationError | None:
    return next(iter(validator.iter_errors(arguments)), None)


def _format_validation_error(error: ValidationError) -> str:
    if error.absolute_path:
        path = ".".join(str(part) for part in error.absolute_path)
        return f"{path}: {error.message}"

    return error.message


async def get_tool_input_schema(
    mcp: McpManager,
    tool_name: str,
) -> dict[str, Any] | None:
    """
    Find a tool's input schema from the OpenAI-style tool definitions
    cached by McpManager.
    """
    available_tools = await mcp.available_tools()

    for tool in available_tools:
        if not isinstance(tool, dict):
            continue

        function = tool.get("function")
        if not isinstance(function, dict):
            continue

        if function.get("name") != tool_name:
            continue

        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            return parameters

    return None


def code_variant_content(
    raw_arguments: str,
    normalized_arguments: str | None = None,
) -> str:
    """Best-effort to send valid json string to client"""
    if normalized_arguments is not None:
        return normalized_arguments

    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return json.dumps({"code": ""})

    if not isinstance(parsed, dict):
        return json.dumps({"code": ""})

    direct_code = parsed.get("code")
    if isinstance(direct_code, str):
        return json.dumps({"code": direct_code})

    for value in parsed.values():
        if not isinstance(value, dict):
            continue

        nested_code = value.get("code")
        if isinstance(nested_code, str):
            return json.dumps({"code": nested_code})

    return json.dumps({"code": ""})


# ──────────────────────────────────────────────────────────────────────────────
# Tool result parsers
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class FinalSummary:
    var_block: list[StreamVariant]
    tool_messages: list[OpenAIMessage]
    is_error: bool


def parse_tool_result(
    resp_txt: str,
    tool_name: str,
    call_id: str,
    thread_id: str,
    include_images: bool,
):
    result_json = json.loads(resp_txt)
    toolout_v: StreamVariant

    structured_content = result_json.get("structuredContent")
    if structured_content is not None:
        if tool_name == "code_interpreter":
            yield from parse_code_interpreter_result(
                structured_content, call_id, thread_id, include_images
            )
        else:
            yield from parse_generic_tool_result(structured_content, tool_name, call_id)
    else:
        if result_json.get("error"):
            out = result_json.get("error")
        elif isinstance(result_json.get("content", {}), dict):
            out = result_json.get("content", {}).get("text", "Unknown response.")
        else:
            out = result_json.get("content", {})
        out_msg = f"{tool_name} error: {out}"

        if tool_name == "code_interpreter":
            toolout_v = SVCodeOutput(content=normalize_code_output(out_msg), id=call_id)
        else:
            toolout_v = SVToolOutput(
                content=GenericToolResult(error=out_msg),
                tool_name=tool_name,
                id=call_id,
            )
        yield toolout_v
        tool_msg = help_convert_sv_ccrm([toolout_v])
        isError = True
        yield FinalSummary(
            var_block=[toolout_v], tool_messages=tool_msg, is_error=isError
        )


def parse_code_interpreter_result(
    result_dict: dict,
    id: str,
    thread_id: str,
    include_images: bool = True,
):
    result = CodeInterpreterResult.model_validate(result_dict)

    code_block: List[StreamVariant] = []
    code_msgs: List[OpenAIMessage] = []

    # Code output: structured dict of displayed data, image or error

    # Check if any file was created
    for file in result.created_files:
        file.preview_url = (
            f"{PROJECT_WEBSITE}/static/preview/climateclaw/{thread_id}/{file.path}"
        )

    if not result.has_output and not result.has_error:
        result.stdout = "Execution completed successfully."

    codeout_v = SVCodeOutput(content=normalize_code_output(result), id=id)
    code_msgs.extend(help_convert_sv_ccrm([codeout_v]))
    code_block.append(codeout_v)
    yield codeout_v

    # Get number of images in "display_data" - contains rich output, image/html/json
    num_display_data_with_png = sum(
        1
        for item in result.display_data
        if isinstance(item, dict) and "image/png" in item
    )

    num_saved_images = sum(
        1 for file in result.created_files if file.mime_type == "image/png"
    )

    # If there are more images streamed than saved, we stream them all to client and model
    if num_display_data_with_png > num_saved_images:
        for i, r in enumerate(result.display_data):
            if "image/png" in r.keys():
                base64_image = r["image/png"]
                image_id = id + f"_{i}"
                image_v = SVImage(content=base64_image, id=image_id)
                yield image_v
                code_block.append(image_v)
                code_msgs.extend(
                    help_convert_sv_ccrm(
                        [
                            SVUser(
                                content="The code interpreter executed successfully and generated "
                                "the requested image. Inspect the image and provide the final "
                                "answer to the user. Do not call the code interpreter again "
                                "unless the image shows that the task failed."
                            ),
                            image_v,
                        ],
                        include_images=include_images,
                    )
                )

    yield FinalSummary(
        var_block=code_block, tool_messages=code_msgs, is_error=result.has_error
    )


def parse_generic_tool_result(result_dict: dict, tool_name: str, id: str):
    result = GenericToolResult.model_validate(result_dict)

    web_sv = SVToolOutput(content=result, tool_name=tool_name, id=id)
    web_msg = help_convert_sv_ccrm([web_sv])
    yield FinalSummary(var_block=[web_sv], tool_messages=web_msg, is_error=False)
