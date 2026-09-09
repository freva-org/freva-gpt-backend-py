import json

import pytest

from climateclaw.services.streaming import stream_orchestrator
from climateclaw.services.streaming.stream_orchestrator import (
    StreamState,
    stream_with_tools,
)
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVCode,
    SVCodeOutput,
    SVStreamEnd,
    SVToolCall,
    SVToolOutput,
)
from conftest import DummyMcpManager, register_fake_mcp


async def fake_assistant_stream():
    yield {
        "choices": [
            {
                "delta": {"content": "hello "},
                "finish_reason": None,
            }
        ]
    }
    yield {
        "choices": [
            {
                "delta": {"content": "world"},
                "finish_reason": "stop",
            }
        ]
    }


def tool_schema(tool_name, properties, required):
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"{tool_name} test tool",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


async def fake_tool_call_stream(tool_name, arguments):
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": arguments,
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


async def collect_stream_with_code_tool(
    *,
    patch_registry,
    patch_thread_storage,
    monkeypatch,
    thread_id,
    model,
    raw_arguments,
):
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    executed_arguments = None

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("code_interpreter", raw_arguments)

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal executed_arguments
        executed_arguments = kwargs["arguments_json"]
        return json.dumps({"structuredContent": {"stdout": "ok"}})

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model=model,
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    return items, executed_arguments


@pytest.mark.asyncio
async def test_stream_with_tools_consumes_async_iterator_response(
    patch_registry, patch_thread_storage
):
    patch_registry({"t-stream": []})

    async def fake_acomplete(**kwargs):
        assert kwargs["stream"] is True
        return fake_assistant_stream()

    items = [
        item
        async for item in stream_with_tools(
            model="test-model",
            thread_id="t-stream",
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert items == [
        SVAssistant(content="hello "),
        SVAssistant(content="world"),
        SVStreamEnd(content="Stream ended."),
    ]


@pytest.mark.asyncio
async def test_stream_with_tools_rejects_malformed_code_arguments_without_mcp_call(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-malformed-code"
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    tool_was_called = False

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("code_interpreter", '{"source": "print(1)"}')

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal tool_was_called
        tool_was_called = True
        return "{}"

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gemma4:31b",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert tool_was_called is False
    assert SVCode(content='{"code": ""}', id="call_1") in items
    code_outputs = [item for item in items if isinstance(item, SVCodeOutput)]
    assert len(code_outputs) == 1
    assert "Invalid code_interpreter arguments" in str(code_outputs[0].content)


@pytest.mark.asyncio
async def test_stream_with_tools_streams_raw_code_chunks_for_non_ollama_models(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    # OpenAI models can stream tool-call arguments incrementally, so the backend
    # forwards those raw chunks to the client as soon as they arrive. The arguments
    # are still normalized later before tool execution for every model. In theory,
    # if an OpenAI model streamed malformed arguments like in this test, the client
    # could fail before normalization happens. However, we keep the existing streaming
    # behavior because this has not occurred with OpenAI models in practice.

    raw_arguments = '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}'

    items, executed_arguments = await collect_stream_with_code_tool(
        patch_registry=patch_registry,
        patch_thread_storage=patch_thread_storage,
        monkeypatch=monkeypatch,
        thread_id="t-non-ollama-code",
        model="gpt-4.1",
        raw_arguments=raw_arguments,
    )

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert [item for item in items if isinstance(item, SVCode)] == [
        SVCode(content=raw_arguments, id="call_1")
    ]


@pytest.mark.parametrize(
    "model",
    [
        "gemma4:31b",
        "qwen3.6:latest",
        "mistral-small:latest",
    ],
)
@pytest.mark.asyncio
async def test_stream_with_tools_emits_normalized_code_later_for_ollama_models(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
    model,
):
    raw_arguments = '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}'

    items, executed_arguments = await collect_stream_with_code_tool(
        patch_registry=patch_registry,
        patch_thread_storage=patch_thread_storage,
        monkeypatch=monkeypatch,
        thread_id=f"t-ollama-code-{model}",
        model=model,
        raw_arguments=raw_arguments,
    )

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert [item for item in items if isinstance(item, SVCode)] == [
        SVCode(content='{"code": "print(1)"}', id="call_1")
    ]


@pytest.mark.asyncio
async def test_stream_with_tools_normalizes_wrapped_arguments_before_tool_execution(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-wrapped-tool"
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "code_interpreter",
                    {"code": {"type": "string"}},
                    ["code"],
                )
            ]
        ),
    )
    executed_arguments = None

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream(
            "code_interpreter",
            '{"args": {"code": "print(1)"}, "tool": "code_interpreter"}',
        )

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal executed_arguments
        executed_arguments = kwargs["arguments_json"]
        return json.dumps({"structuredContent": {"result": "ok"}})

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gemma4:31b",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert json.loads(executed_arguments) == {"code": "print(1)"}
    assert (
        SVCode(
            content='{"code": "print(1)"}',
            id="call_1",
        )
        in items
    )


@pytest.mark.asyncio
async def test_stream_with_tools_preserves_raw_invalid_non_code_tool_call(
    patch_registry,
    patch_thread_storage,
    monkeypatch,
):
    thread_id = "t-invalid-web-search"
    raw_arguments = '{"q": "climate data"}'
    register_fake_mcp(
        patch_registry,
        thread_id,
        DummyMcpManager(
            [
                tool_schema(
                    "web_search",
                    {"query": {"type": "string"}},
                    ["query"],
                )
            ]
        ),
    )
    tool_was_called = False

    async def fake_acomplete(**kwargs):
        return fake_tool_call_stream("web_search", raw_arguments)

    async def fake_run_tool_via_mcp(**kwargs):
        nonlocal tool_was_called
        tool_was_called = True
        return "{}"

    monkeypatch.setattr(
        stream_orchestrator,
        "run_tool_via_mcp",
        fake_run_tool_via_mcp,
    )

    items = [
        item
        async for item in stream_with_tools(
            model="gpt-5",
            thread_id=thread_id,
            messages=[],
            acomplete_func=fake_acomplete,
            stream_state=StreamState(),
            storage=patch_thread_storage,
            store_thread=False,
        )
    ]

    assert tool_was_called is False
    assert (
        SVToolCall(
            content=raw_arguments,
            id="call_1",
            tool_name="web_search",
        )
        in items
    )
    tool_outputs = [item for item in items if isinstance(item, SVToolOutput)]
    assert len(tool_outputs) == 1
    assert "Invalid arguments for tool web_search" in str(tool_outputs[0].content)
