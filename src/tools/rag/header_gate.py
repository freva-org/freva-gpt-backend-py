from typing import Callable, Awaitable, Dict, Any
from contextvars import ContextVar
import logging

VAULT_URI_HDR = "x-freva-vault-url"
REST_URL_HDR  = "x-freva-rest-url"

def make_header_gate(
    inner_app,
    *,
    vault_ctx: ContextVar[str | None],
    rest_ctx:  ContextVar[str | None],
    logger: logging.Logger | None = None,
    mcp_path: str = "/mcp",
):
    """
    Wrap the FastMCP ASGI app so every request to `mcp_path`:
      - logs x-freva-* headers,
      - enforces a valid mongodb URI in x-freva-vault-url,
      - sets ContextVars for downstream code.
    """
    log = logger or logging.getLogger("rag.header_gate")

    class HeaderCaptureASGI:
        def __init__(self, app):
            self.app = app

        async def __call__(
            self,
            scope: Dict[str, Any],
            receive: Callable[..., Awaitable],
            send: Callable[..., Awaitable],
        ):
            if scope.get("type") != "http":
                return await self.app(scope, receive, send)

            path = scope.get("path", "")
            if path != mcp_path:
                return await self.app(scope, receive, send)

            # Normalize headers to a case-insensitive dict
            hdrs = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            v = hdrs.get(VAULT_URI_HDR)
            r = hdrs.get(REST_URL_HDR)

            try:
                log.info("RAG headers (ASGI wrap): vault=%r rest=%r", v, r)
            except Exception:
                pass  # never fail on logging

            # Enforce required vault header
            if not v or not (v.startswith("mongodb://") or v.startswith("mongodb+srv://")):
                body = (
                    b'event: message\r\n'
                    b'data: {"jsonrpc":"2.0","error":{"code":-32600,'
                    b'"message":"Missing or invalid header \'' + VAULT_URI_HDR.encode("utf-8") + b'\' '
                    b'(expected mongodb:// or mongodb+srv://)"}}\r\n\r\n'
                )
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-cache, no-transform"),
                        (b"connection", b"keep-alive"),
                    ],
                })
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return

            # Set ContextVars for downstream code
            tok_v = vault_ctx.set(v)
            tok_r = rest_ctx.set(r) if r is not None else None
            try:
                return await self.app(scope, receive, send)
            finally:
                vault_ctx.reset(tok_v)
                if tok_r is not None:
                    rest_ctx.reset(tok_r)

    return HeaderCaptureASGI(inner_app)
