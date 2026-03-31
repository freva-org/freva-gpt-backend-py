from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple
import asyncio

import httpx

from src.core.logging_setup import configure_logging
from src.core.settings import get_settings

DEFAULT_LOGGER = configure_logging(__name__)
settings = get_settings()

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_CLIENT_INFO = {"name": "freva-backend", "version": "local"}
DEFAULT_SESSION_KEY = "__default__"

@dataclass
class McpCallResult:
    ok: bool
    id: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    status_code: Optional[int] = None


class McpError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rpc_code: int | None = None,
        payload: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.rpc_code = rpc_code
        self.payload = payload


class McpUnauthorized(McpError): ...


class McpBadRequest(McpError): ...


class McpInvalidParams(McpError): ...


class McpMethodNotFound(McpError): ...
class McpRequestCancelled(McpError): ...


class McpClient:
    """
    Minimal JSON-RPC over HTTP client with SSE support (FastMCP style).
    """

    def __init__(
        self,
        base_url: str,
        *,
        default_headers: Optional[Dict[str, str]] = None,
        logger=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self._lock = asyncio.Lock()
        self._session_id: str | None = None
        self.log = logger or DEFAULT_LOGGER

        # simple shared client
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.MCP_REQUEST_TIMEOUT_SEC),
        )

        self._active_request_id: str | None = None
        self._pending_request_id: str | None = None
        self._pending_local_cancel: bool = False

    # ────────── session ──────────

    async def _ensure_session(self) -> str:
        async with self._lock:
            if self._session_id:
                self.log.debug("Reusing MCP session sid=%s", self._session_id)
                return self._session_id

            self._session_id = await self._start_session()
            self.log.debug("Created MCP session sid=%s", self._session_id)
            return self._session_id

    async def _start_session(self) -> str:
        init_body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": DEFAULT_CLIENT_INFO,
            },
        }
        init_resp = await self._http.post(
            "/mcp", headers=self._headers(include_session=False), json=init_body
        )
        init_payload, sid = self._extract_payload_and_session(init_resp)

        if not sid:
            raise McpError(
                "MCP server did not include a session id during initialize()",
                status_code=init_resp.status_code,
                payload=init_payload,
            )

        self._raise_for_error_payload(init_resp, init_payload)

        notify_body = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        notify_resp = await self._http.post(
            "/mcp", headers=self._headers(session_id=sid), json=notify_body
        )
        notify_payload, _ = self._extract_payload_and_session(notify_resp)
        self._raise_for_error_payload(notify_resp, notify_payload)

        return sid

    def _extract_payload_and_session(
        self, response: httpx.Response
    ) -> Tuple[Optional[Any], Optional[str]]:
        session_id = response.headers.get("Mcp-Session-Id")
        payload: Optional[Any] = None
        content_type = response.headers.get("content-type", "")

        try:
            if "text/event-stream" in content_type:
                text = response.text
                data_lines = []

                for line in text.splitlines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line.strip() and data_lines:
                        # end of SSE event
                        break

                if data_lines:
                    payload = json.loads("\n".join(data_lines))

            elif response.content:
                try:
                    payload = response.json()
                except Exception:
                    # fallback for non-JSON bodies (auth errors etc.)
                    payload = response.text

        except Exception as e:
            self.log.exception("Invalid MCP response payload")
            raise McpError(
                f"Invalid MCP response payload: {e}",
                status_code=response.status_code,
            ) from e

        return payload, session_id

    # ────────── headers ──────────

    def _headers(
        self,
        extra: Optional[Dict[str, str]] = None,
        *,
        include_session: bool = True,
        session_id: str | None = None,
    ) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        h.update(self.default_headers)

        if include_session and session_id:
            h["Mcp-Session-Id"] = session_id

        if extra:
            h.update(extra)
        return drop_none(h)

    # ────────── tool discovery ──────────

    async def tools_list_rpc(self) -> McpCallResult:
        """
        Try JSON-RPC method: 'tools/list' (default) or 'tools.list' (dot_name=True).
        """
        session_id = await self._ensure_session()
        rpc_id = str(uuid.uuid4())

        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/list",
            "params": {"cursor": None},
        }

        r = await self._http.post(
            "/mcp", headers=self._headers(session_id=session_id), json=body
        )
        return self._rpc_result(r, rpc_id)

    async def tools_list_http(self) -> List[Dict[str, Any]]:
        """
        Try plain HTTP GET /tools (some implementations expose this).
        """
        r = await self._http.get("/tools", headers=self._headers(include_session=False))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "tools" in data:
            return data["tools"]
        if isinstance(data, list):
            return data
        return []

    # ────────── call tool ──────────

    async def call_tool(
        self,
        *,
        name: str,
        args: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Call a tool via JSON-RPC method 'tools/call'.
        Sets session id based on session_key to keep continuity.
        """
        rpc_id = str(uuid.uuid4())
        
        # publish request identity immediately, before any await
        async with self._lock:
            if self._active_request_id is not None or self._pending_request_id is not None:
                raise McpError("Tool call already in progress")
            self._pending_request_id = rpc_id
            self._pending_local_cancel = False

        try:
            session_id = await self._ensure_session()

            body = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }

            async with self._lock:
                if self._pending_request_id != rpc_id:
                    raise McpError("Pending request state corrupted")

                # cancel arrived before send: abort locally
                if self._pending_local_cancel:
                    raise McpRequestCancelled("Request cancelled before send")

                # promote pending -> active right before POST
                self._pending_request_id = None
                self._active_request_id = rpc_id

            try:
                r = await self._http.post(
                    "/mcp",
            headers=self._headers(extra_headers, session_id=session_id),
            json=body,
                )
                res = self._rpc_result(r, rpc_id)
                if isinstance(res.result, dict):
                    return res.result
                return {"result": res.result}
            finally:
                async with self._lock:
                    if self._active_request_id == rpc_id:
                        self._active_request_id = None
        finally:
            async with self._lock:
                if self._pending_request_id == rpc_id:
                    self._pending_request_id = None
                if self._active_request_id is None and self._pending_request_id is None:
                    self._pending_local_cancel = False

    # ────────── rpc result helper ──────────

    def _rpc_result(self, response: httpx.Response, rpc_id: str) -> McpCallResult:
        payload, session_id = self._extract_payload_and_session(response)
        self._raise_for_error_payload(response, payload)

        if isinstance(payload, dict):
            return McpCallResult(
                ok=True,
                id=rpc_id,
                result=payload.get("result"),
                status_code=response.status_code,
            )

        return McpCallResult(
            ok=True, id=rpc_id, result=payload, status_code=response.status_code
        )

    def _raise_for_error_payload(self, response: httpx.Response, payload: Any) -> None:
        if not isinstance(payload, dict):
            # HTTP / auth / malformed server response
            if response.status_code == 401:
                # e.g. HTTP/1.1 401 Unauthorized
                raise McpUnauthorized(
                    "Unauthorized",
                    status_code=response.status_code,
                    payload=payload,
                )
            if response.status_code >= 400:
                # e.g. HTTP/1.1 500 Internal Server Error
                raise McpError(
                    f"HTTP {response.status_code}",
                    status_code=response.status_code,
                    payload=payload,
                )
            return

        err = payload.get("error")
        if not err:
            # Seems like success but check status code
            if response.status_code == 401:
                raise McpUnauthorized(
                    "Unauthorized",
                    status_code=response.status_code,
                    payload=payload,
                )
            if response.status_code >= 400:
                raise McpError(
                    f"HTTP {response.status_code}",
                    status_code=response.status_code,
                    payload=payload,
                )
            return

        # Standard JSON-RPC error
        if err and isinstance(err, dict):
            rpc_code = err.get("code")
            message = err.get("message", str(err))

            if response.status_code == 401:
                raise McpUnauthorized(
                    message,
                    status_code=response.status_code,
                    rpc_code=rpc_code,
                    payload=payload,
                )
            if rpc_code == -32601:
                raise McpMethodNotFound(
                    message,
                    status_code=response.status_code,
                    rpc_code=rpc_code,
                    payload=payload,
                )
            if rpc_code == -32602:
                raise McpInvalidParams(
                    message,
                    status_code=response.status_code,
                    rpc_code=rpc_code,
                    payload=payload,
                )

            raise McpError(
                message,
                status_code=response.status_code,
                rpc_code=rpc_code,
                payload=payload,
            )

        # Non-standard error shape: similar to first case
        message = str(err)
        if response.status_code == 401 or message in {"invalid token", "invalid_token"}:
            raise McpUnauthorized(
                message,
                status_code=response.status_code,
                payload=payload,
            )

        raise McpError(
            message,
            status_code=response.status_code,
            payload=payload,
        )

    # ────────── request cancellation ──────────

    async def cancel_request(self, reason: str | None = "client_cancelled") -> None:
        async with self._lock:
            session_id = self._session_id
            active_request_id = self._active_request_id
            pending_request_id = self._pending_request_id

            # request exists locally but not yet sent: cancel locally
            if pending_request_id is not None and active_request_id is None:
                self._pending_local_cancel = True
                return

        request_id = active_request_id

        # nothing to cancel
        if not session_id or not request_id:
            return

        resp = await self._http.request(
            "POST",
            "/mcp",
            headers=self._headers(
                session_id=session_id,
                extra={"Mcp-Request-Id": request_id,
                       "Mcp-Cancel": "true",}
            )
        )
        if resp.status_code not in (200, 202, 204):
            raise McpBadRequest(
                f"cancel_request failed: HTTP {resp.status_code} body={resp.text!r}",
                status_code=resp.status_code,
                payload=resp.text,
            )
        
    # ────────── termination and clean-up ──────────

    async def terminate_session(self) -> None:
        """
        Best-effort: tell server to terminate the current session via HTTP DELETE.
        Server is expected to identify the session via Mcp-Session-Id header.
        """
        async with self._lock:
            sid = self._session_id

        if not sid:
            return
        
        try:
            await self._http.delete("/mcp", headers=self._headers(session_id=sid))
        except Exception:
            self.log.exception("Failed to terminate MCP session sid=%s", sid, exc_info=True)

    async def close(self) -> None:
        try:
            await self.terminate_session()
        finally:
            async with self._lock:
                self._session_id = None
                self._active_request_id = None
            await self._http.close()


# ──────────────────── Helper functions ──────────────────────────────


def drop_none(d: dict[str, str]) -> dict[str, str]:
    """Remove keys from d whose value is None."""
    return {k: v for k, v in d.items() if v is not None}
