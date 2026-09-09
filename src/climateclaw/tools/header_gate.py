import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Optional

from mcp.server.streamable_http import ServerMessageMetadata
from mcp.shared.session import RequestResponder

from climateclaw.core.logging_setup import (
    REQUEST_ID_HEADER,
    configure_logging,
    reset_request_id,
    set_request_id,
)

log = logging.getLogger(__name__)
configure_logging()

_MCP_REQUEST_ID_CONTEXT_PATCHED = False


def _debug_request_id_from_mcp_message(message) -> str | None:
    if not isinstance(message, RequestResponder):
        return None

    metadata = getattr(message, "message_metadata", None)
    if not isinstance(metadata, ServerMessageMetadata):
        return None

    request = getattr(metadata, "request_context", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None

    return headers.get(REQUEST_ID_HEADER)


def _patch_debug_request_id_context() -> None:
    """
    Restore X-Request-Id per MCP JSON-RPC message.

    Streamable HTTP keeps one server task per stateful MCP session. That task is
    created when the session is initialized, so task-local ContextVars otherwise
    keep the request id from session creation for later tool calls.
    """
    global _MCP_REQUEST_ID_CONTEXT_PATCHED
    if _MCP_REQUEST_ID_CONTEXT_PATCHED:
        return

    from mcp.server.lowlevel.server import Server

    original_handle_message = Server._handle_message

    async def handle_message_with_request_id_context(self, message, *args, **kwargs):
        request_id = _debug_request_id_from_mcp_message(message)
        token = set_request_id(request_id) if request_id else None
        try:
            return await original_handle_message(self, message, *args, **kwargs)
        finally:
            if token is not None:
                reset_request_id(token)

    setattr(Server, "_handle_message", handle_message_with_request_id_context)
    _MCP_REQUEST_ID_CONTEXT_PATCHED = True


async def _send_response(
    send: Callable[..., Awaitable],
    *,
    status: int,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers or [],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_json_response(
    send: Callable[..., Awaitable],
    *,
    status: int,
    body: bytes,
) -> None:
    await _send_response(
        send,
        status=status,
        headers=[(b"content-type", b"application/json")],
        body=body,
    )


async def _send_invalid_mongodb_header_response(
    send: Callable[..., Awaitable],
    header_name: str,
) -> None:
    body = (
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0","error":{"code":-32600,'
        b'"message":"Missing or invalid header \'' + header_name.encode("utf-8") + b"' "
        b'(expected mongodb:// or mongodb+srv://)"}}\r\n\r\n'
    )
    await _send_response(
        send,
        status=400,
        headers=[
            (b"content-type", b"text/event-stream"),
            (b"cache-control", b"no-cache, no-transform"),
            (b"connection", b"keep-alive"),
        ],
        body=body,
    )


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    # Normalize headers to a case-insensitive dict
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }


def make_header_gate(
    inner_app,
    *,
    ctx_list: list[ContextVar[str | None]],
    header_name_list: list[str],
    logger: logging.Logger | logging.LoggerAdapter | None = None,
    mcp_path: str = "/mcp",
    on_session_close: Callable[[str], None] | None = None,
    on_cancel_request: Optional[Callable[[str, str], Awaitable[None]]] = None,
):
    """
    Wrap the FastMCP ASGI app so every request to `mcp_path`:
      - enforces a valid mongodb URI in mongodb-uri,
      - sets ContextVars for downstream code.
      - (optional) cleans up on DELETE {mcp_path} using Mcp-Session-Id.
    """
    _patch_debug_request_id_context()
    log = logger or logging.getLogger("header_gate")

    class HeaderCaptureASGI:
        def __init__(self, app):
            self.app = app

        async def __call__(
            self,
            scope: dict[str, Any],
            receive: Callable[..., Awaitable],
            send: Callable[..., Awaitable],
        ):
            if scope.get("type") != "http":
                return await self.app(scope, receive, send)

            path = scope.get("path", "")

            if path == "/healthz":
                body = json.dumps({"status": "ok"}).encode("utf-8")
                await _send_json_response(send, status=200, body=body)
                return

            if path != mcp_path:
                return await self.app(scope, receive, send)

            hdrs = _headers_from_scope(scope)

            # This is the frontend/proxy request ID. The logging filter reads
            # it from the ContextVar; it is different from Mcp-Request-Id below.
            request_id_token = set_request_id(hdrs.get(REQUEST_ID_HEADER.lower()))

            method = (scope.get("method") or "").upper()
            mcp_session_id = hdrs.get("mcp-session-id", "")

            try:
                # handle session close
                if method == "DELETE":
                    try:
                        log.info(
                            "DELETE %s received. session_id=%r",
                            mcp_path,
                            mcp_session_id,
                        )
                    except Exception:
                        pass

                    if on_session_close and mcp_session_id:
                        try:
                            on_session_close(mcp_session_id)
                        except Exception:
                            log.exception(
                                "on_session_close failed for sid=%s", mcp_session_id
                            )

                    await _send_response(send, status=204)
                    return

                # handle session interrupt
                if method == "POST" and "mcp-cancel" in hdrs:
                    mcp_request_id = hdrs.get("mcp-request-id", "")

                    try:
                        log.info(
                            "CANCEL %s received. session_id=%r request_id=%r",
                            mcp_path,
                            mcp_session_id,
                            mcp_request_id,
                        )
                    except Exception:
                        pass

                    if not mcp_session_id or not mcp_request_id:
                        await _send_json_response(
                            send,
                            status=400,
                            body=b'{"error":"Missing Mcp-Session-Id or Mcp-Request-Id"}',
                        )
                        return

                    if on_cancel_request:
                        try:
                            await on_cancel_request(mcp_session_id, mcp_request_id)
                        except Exception:
                            log.exception(
                                "on_cancel_request failed for sid=%s request_id=%s",
                                mcp_session_id,
                                mcp_request_id,
                            )

                    await _send_response(send, status=204)
                    return

                tokens: list[tuple[ContextVar, Any]] = []

                try:
                    for ctx, header_name in zip(ctx_list, header_name_list):
                        v = hdrs.get(header_name)

                        if header_name == "mongodb-uri" and (
                            not v
                            or not (
                                v.startswith("mongodb://")
                                or v.startswith("mongodb+srv://")
                            )
                        ):
                            await _send_invalid_mongodb_header_response(
                                send, header_name
                            )
                            return

                        tok_v = ctx.set(v)
                        tokens.append((ctx, tok_v))

                    return await self.app(scope, receive, send)

                finally:
                    for ctx, tok_v in reversed(tokens):
                        ctx.reset(tok_v)
            finally:
                reset_request_id(request_id_token)

    return HeaderCaptureASGI(inner_app)
