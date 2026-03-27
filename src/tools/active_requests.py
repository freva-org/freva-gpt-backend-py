from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import asyncio

from fastmcp.server.dependencies import get_context


class RequestCancelled(Exception):
    """Raised when an in-flight MCP request was cancelled by the client."""


@dataclass
class ActiveRequest:
    session_id: str
    request_id: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def cancel(self) -> None:
        self.cancelled.set()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()
    
    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise RequestCancelled(
                f"Request cancelled by client: session_id={self.session_id!r} request_id={self.request_id!r}"
            )


class ActiveRequestRegistry:
    """
    Tracks only in-flight MCP requests.

    Keyed by (session_id, request_id), so it can be reused by any MCP server:
    code-server, web-search, rag, etc.
    """
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], ActiveRequest] = {}

    async def start(self, session_id: str, request_id: str) -> ActiveRequest:
        key = (session_id, request_id)
        async with self._lock:
            req = self._requests.get(key)
            if req is None:
                req = ActiveRequest(session_id=session_id, request_id=request_id)
                self._requests[key] = req
            return req

    async def cancel(self, session_id: str, request_id: str) -> None:
        """
        Mark request as cancelled.

        If CANCEL arrives before start(), preserve that state so start()
        does not accidentally overwrite it.
        """
        key = (session_id, request_id)
        async with self._lock:
            req = self._requests.get(key)
            if req is None:
                req = ActiveRequest(session_id=session_id, request_id=request_id)
                req.cancel()
                self._requests[key] = req
            else:
                req.cancel()

    async def end(self, session_id: str, request_id: str) -> None:
        async with self._lock:
            self._requests.pop((session_id, request_id), None)

    async def is_cancelled(self, session_id: str, request_id: str) -> bool:
        async with self._lock:
            req = self._requests.get((session_id, request_id))
            return False if req is None else req.is_cancelled()

    async def get(self, session_id: str, request_id: str) -> ActiveRequest | None:
        async with self._lock:
            return self._requests.get((session_id, request_id))


ACTIVE_REQUESTS = ActiveRequestRegistry()


def current_ids() -> tuple[str, str]:
    ctx = get_context()
    sid = getattr(ctx, "session_id", "")
    rid = getattr(ctx, "request_id", "")
    if not sid or not rid:
        raise RuntimeError("Missing Mcp-Session-Id or Mcp-Request-Id")
    return sid, rid



@asynccontextmanager
async def tracked_request(session_id: str, request_id: str):
    req = await ACTIVE_REQUESTS.start(session_id, request_id)
    try:
        yield req
    finally:
        await ACTIVE_REQUESTS.end(session_id, request_id)