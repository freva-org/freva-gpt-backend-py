from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import asyncio
import threading

from fastmcp.server.dependencies import get_context


class RequestCancelled(Exception):
    """Raised when an in-flight MCP request was cancelled by the client."""


@dataclass
class ActiveRequest:
    session_id: str
    request_id: str
    cancelled_async: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled_thread: threading.Event = field(default_factory=threading.Event)
    execute_sent: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self.cancelled_async.set()
        self.cancelled_thread.set()

    def is_cancelled(self) -> bool:
        return self.cancelled_thread.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
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
            # In the current setup, only one active request per session_id is allowed.
            # We keep the current request entry if it already exists, therefore preserving
            # pre-cancel-before-start, and remove any older stale entries for the same session_id.
            stale_keys = [
                k for k in self._requests.keys() if k[0] == session_id and k != key
            ]  # collects only the entries with same session_id and different request_id
            for k in stale_keys:
                del self._requests[k]

            req = self._requests.get(key)
            if req is None:
                # In case the request has not been registered yet, this records pre-cancel-before-start.
                # But it would also register cancel requests that arrive late and while unlikely,
                # it could blow up the registry. To prevent this, we do a cleanup for stale entries above.
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
