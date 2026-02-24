import pytest
from contextvars import ContextVar

@pytest.mark.asyncio
async def test_header_gate_delete_triggers_cleanup_and_returns_204():
    # Import your make_header_gate from wherever it lives
    # from src.tools.header_gate import make_header_gate
    from src.tools.header_gate import make_header_gate

    # Dummy inner app that should NOT be called on DELETE
    inner_called = {"called": False}
    async def inner_app(scope, receive, send):
        inner_called["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK", "more_body": False})

    # Capture cleanup callback calls
    cleanup_called = {"sid": None}
    def on_session_close(sid: str):
        cleanup_called["sid"] = sid

    # ContextVar can be anything; it should not matter on DELETE
    cwd_ctx: ContextVar[str | None] = ContextVar("cwd_ctx", default=None)

    app = make_header_gate(
        inner_app,
        ctx_list=[cwd_ctx],
        header_name_list=["working-dir"],
        mcp_path="/mcp",
        on_session_close=on_session_close,
    )

    # Minimal ASGI plumbing
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []
    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "DELETE",
        "path": "/mcp",
        "headers": [
            (b"Mcp-Session-Id", b"abc123"),
        ],
    }

    await app(scope, receive, send)

    # Cleanup was called with the session ID
    assert cleanup_called["sid"] == "abc123"

    # Inner app should NOT have been called
    assert inner_called["called"] is False

    # Response is 204 No Content
    assert any(m["type"] == "http.response.start" and m["status"] == 204 for m in sent)
    assert any(m["type"] == "http.response.body" and m["body"] == b"" for m in sent)
