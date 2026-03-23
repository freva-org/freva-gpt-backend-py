from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple
import threading

import httpx

from src.core.logging_setup import configure_logging
from src.core.settings import get_settings

DEFAULT_LOGGER = configure_logging(__name__)
settings = get_settings()

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_CLIENT_INFO = {"name": "freva-backend", "version": "local"}
DISCOVERY_SESSION_KEY = "__discovery__"


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


class McpClient:
    """
    Minimal JSON-RPC over HTTP client with SSE support (FastMCP style).
    """

    def __init__(
        self, 
        base_url: str,
        *, 
        default_headers: Optional[Dict[str, str]] = None, 
        logger=None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self._lock = threading.RLock()
        self._session_ids: Dict[str, str] = {}
        self.log = logger or DEFAULT_LOGGER

        # simple shared client
        self._http = httpx.Client(
            base_url=self.base_url, 
            timeout=httpx.Timeout(settings.MCP_REQUEST_TIMEOUT_SEC)
        )

    # ────────── session ──────────

    def _ensure_session(self, logical_key: str) -> str:
        lk = logical_key or "__anon__"
        with self._lock:
            existing = self._session_ids.get(lk)
            if existing:
                return existing

            new_sid = self._start_session()
            self._session_ids[lk] = new_sid
            return new_sid


    def _start_session(self) -> str:
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
        init_resp = self._http.post("/mcp", headers=self._headers(include_session=False), 
                                    json=init_body)
        init_payload, sid = self._extract_payload_and_session(init_resp)

        if not sid:
            raise McpError("MCP server did not include a session id during initialize()",
                           status_code=init_resp.status_code,
                           payload=init_payload
                           )
        
        self._raise_for_error_payload(init_resp, init_payload)

        notify_body = {
            "jsonrpc": "2.0", 
            "method": "notifications/initialized", 
            "params": {}
        }
        notify_resp = self._http.post(
            "/mcp", 
            headers=self._headers(session_id=sid), 
            json=notify_body
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
        self, extra: Optional[Dict[str, str]] = None, 
        *, include_session: bool = True,
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

    def tools_list_rpc(self) -> McpCallResult:
        """
        Try JSON-RPC method: 'tools/list' (default) or 'tools.list' (dot_name=True).
        """
        session_id = self._ensure_session(DISCOVERY_SESSION_KEY)
        rpc_id = str(uuid.uuid4())

        body = {
            "jsonrpc": "2.0", 
            "id": rpc_id, 
            "method": "tools/list", 
            "params": {"cursor": None},
            }
        
        r = self._http.post("/mcp", headers=self._headers(session_id=session_id), json=body)
        return self._rpc_result(r, rpc_id)


    def tools_list_http(self) -> List[Dict[str, Any]]:
        """
        Try plain HTTP GET /tools (some implementations expose this).
        """
        r = self._http.get("/tools", headers=self._headers(include_session=False))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "tools" in data:
            return data["tools"]
        if isinstance(data, list):
            return data
        return []

    # ────────── call tool ──────────

    def call_tool(
        self,
        *,
        name: str,
        args: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
        logical_session_key: str = "__default__",
    ) -> Dict[str, Any]:
        """
        Call a tool via JSON-RPC method 'tools/call'.
        Sets session id based on session_key to keep continuity.
        """
        session_id = self._ensure_session(logical_session_key)

        # JSON-RPC tools/call
        rpc_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        r = self._http.post(
            "/mcp", headers=self._headers(extra_headers, session_id=session_id), json=body
        )
        res = self._rpc_result(r, rpc_id)
        if isinstance(res.result, dict):
            return res.result
        return {"result": res.result}


    # ────────── rpc result helper ──────────

    def _rpc_result(self, response: httpx.Response, rpc_id: str) -> McpCallResult:
        payload, session_id = self._extract_payload_and_session(response)
        self._raise_for_error_payload(response, payload)

        if isinstance(payload, dict):
            return McpCallResult(ok=True, id=rpc_id, result=payload.get("result"), 
                                 status_code=response.status_code)

        return McpCallResult(ok=True, id=rpc_id, result=payload,
                             status_code=response.status_code)
    

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

    # ────────── termination and clean-up ──────────

    def terminate_session(self) -> None:
        """
        Best-effort: tell server to terminate the current session via HTTP DELETE.
        Server is expected to identify the session via Mcp-Session-Id header.
        """
        with self._lock:
            sids = list(set(self._session_ids.values()))

        for sid in set(sids):
            try:
                self._http.delete("/mcp", headers=self._headers(session_id=sid))
            except Exception:
                pass


    def close(self) -> None:
        try:
            self.terminate_session()
        finally:
            with self._lock:
                self._session_ids.clear()
            self._http.close()
        
# ──────────────────── Helper functions ──────────────────────────────

def drop_none(d: dict) -> dict:
    """Remove keys from d whose value is None."""
    return {k: v for k, v in d.items() if v is not None}
