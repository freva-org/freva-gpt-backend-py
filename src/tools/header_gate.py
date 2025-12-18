from typing import Callable, Awaitable, Dict, Any, List
from contextvars import ContextVar
import logging
from src.core.logging_setup import configure_logging

log = logging.getLogger(__name__)
configure_logging()

def make_header_gate(
    inner_app,
    *,
    ctx_list: List[ContextVar[str | None]],
    header_name_list: List[str],
    logger: logging.Logger | None = None,
    mcp_path: str = "/mcp",
):
    """
    Wrap the FastMCP ASGI app so every request to `mcp_path`:
      - enforces a valid mongodb URI in mongodb-uri,
      - sets ContextVars for downstream code.
    """
    log = logger or logging.getLogger("header_gate")

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
            # log.debug(f"Raw server headers: {hdrs}")

            tokens: list[tuple[ContextVar, Any]] = []

            try:
                for ctx, header_name in zip(ctx_list, header_name_list):

                    v = hdrs.get(header_name)

                    try:
                        log.info(f"Server header {header_name} (ASGI wrap): {v}")
                    except Exception:
                        pass  # never fail on logging

                    # Enforce required conditions on header
                    # We do not do the same for CI because it doesn't have to be that strict, it can operate without freva access. We warn about this
                    if header_name=="mongodb-uri" and (not v or not (v.startswith("mongodb://") or v.startswith("mongodb+srv://"))):
                        body = (
                            b'event: message\r\n'
                            b'data: {"jsonrpc":"2.0","error":{"code":-32600,'
                            b'"message":"Missing or invalid header \'' + header_name.encode("utf-8") + b'\' '
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
                    tok_v = ctx.set(v)
                    tokens.append((ctx, tok_v))

                # Call the wrapped app once, with all ContextVars set
                return await self.app(scope, receive, send)

            finally:
                # Reset all ContextVars
                for ctx, tok_v in reversed(tokens):
                    ctx.reset(tok_v)

    return HeaderCaptureASGI(inner_app)
