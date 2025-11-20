from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple
import logging
import threading

import httpx

from src.core.logging_setup import configure_logging

log = logging.getLogger(__name__)
configure_logging()

MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_CLIENT_INFO = {"name": "freva-backend", "version": "local"}
DISCOVERY_SESSION_KEY = "__discovery__"

# ---- public dataclass you can log/inspect -------------------------------------------------------

@dataclass
class McpCallResult:
    ok: bool
    id: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    status_code: Optional[int] = None


# ---- exceptions ---------------------------------------------------------------------------------

class McpError(Exception): ...
class McpUnauthorized(McpError): ...
class McpBadRequest(McpError): ...
class McpInvalidParams(McpError): ...

def drop_none(d: dict) -> None:
    """Remove keys from d whose value is None."""
    return {k: v for k, v in d.items() if v is not None}

# ---- client -------------------------------------------------------------------------------------

class McpClient:
    """
    Minimal JSON-RPC over HTTP client with SSE support (FastMCP style).
    """

    def __init__(self, base_url: str, *, default_headers: Optional[Dict[str, str]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self._session_id: Optional[str] = None
        self._lock = threading.RLock()
        self._session_ids: Dict[str, str] = {}

        # simple shared client
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(300.0))

    # --- session -----------------------------------------------------------------------------

    def _ensure_session(self, logical_key: str) -> str:
        lk = logical_key or "__anon__"
        with self._lock:
            existing = self._session_ids.get(lk)
            if existing:
                self._session_id = existing
                return existing

        new_sid = self._start_session()
        with self._lock:
            self._session_ids[lk] = new_sid
            self._session_id = new_sid
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
        self._session_id = None
        init_resp = self._http.post("/mcp", headers=self._headers(include_session=False), json=init_body)
        init_payload, sid = self._extract_payload_and_session(init_resp)

        if not sid:
            raise McpError("MCP server did not include a session id during initialize()")
        if isinstance(init_payload, dict) and init_payload.get("error"):
            raise McpError(f"MCP initialize failed: {init_payload['error']}")

        self._session_id = sid
        notify_body = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        notify_resp = self._http.post("/mcp", headers=self._headers(), json=notify_body)
        notify_payload, _ = self._extract_payload_and_session(notify_resp)
        if isinstance(notify_payload, dict) and notify_payload.get("error"):
            raise McpError(f"MCP notifications/initialized failed: {notify_payload['error']}")

        return sid

    def _extract_payload_and_session(self, response: httpx.Response) -> Tuple[Optional[Any], Optional[str]]:
        session_id = response.headers.get("mcp-session-id")
        payload: Optional[Any] = None
        content_type = response.headers.get("content-type", "")

        try:
            if "text/event-stream" in content_type:
                text = response.text
                for line in text.splitlines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data:
                            payload = json.loads(data)
                            break
            elif response.content:
                payload = response.json()
        except Exception as e:
            raise McpError(f"Invalid MCP response payload: {e}") from e

        return payload, session_id

    # --- headers ------------------------------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None, *, include_session: bool = True) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        h.update(self.default_headers)
        if include_session and self._session_id:
            h["Mcp-Session-Id"] = self._session_id
            h["Mcp-Protocol-Version"] = MCP_PROTOCOL_VERSION
        if extra:
            h.update(extra)
        return drop_none(h)

    # --- tool discovery -----------------------------------------------------------------------

    def tools_list_rpc(self, *, dot_name: bool = False) -> McpCallResult:
        """
        Try JSON-RPC method: 'tools/list' (default) or 'tools.list' (dot_name=True).
        """
        rpc_id = str(uuid.uuid4())
        self._ensure_session(DISCOVERY_SESSION_KEY)
        rpc_id = str(uuid.uuid4())
        method = "tools.list" if dot_name else "tools/list"
        body = {
            "jsonrpc": "2.0", 
            "id": rpc_id, 
            "method": method, 
            "params": {"cursor": None},
            }
        r = self._http.post("/mcp", headers=self._headers(), json=body)
        return self._rpc_result(r, rpc_id)

    def tools_list_http(self) -> List[Dict[str, Any]]:
        """
        Try plain HTTP GET /tools (some implementations expose this).
        """
        r = self._http.get("/tools", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "tools" in data:
            return data["tools"]
        if isinstance(data, list):
            return data
        return []

    # --- call tool ----------------------------------------------------------------------------

    def call_tool(
        self,
        *,
        name: str,
        args: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Call a tool via JSON-RPC method 'tools/call' or fallbacks.
        Sets session id based on session_key to keep continuity.
        """
        # Strategy 1: JSON-RPC tools/call
        rpc_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        r = self._http.post("/mcp", headers=self._headers(extra_headers), json=body)
        res = self._rpc_result(r, rpc_id)
        if res.ok and isinstance(res.result, dict):
            return res.result

        # Strategy 2: JSON-RPC tools.call
        rpc_id2 = str(uuid.uuid4())
        body2 = {
            "jsonrpc": "2.0",
            "id": rpc_id2,
            "method": "tools.call",
            "params": {"name": name, "arguments": args},
        }
        r2 = self._http.post("/mcp", headers=self._headers(extra_headers), json=body2)
        res2 = self._rpc_result(r2, rpc_id2)
        if res2.ok and isinstance(res2.result, dict):
            return res2.result

        # Strategy 3: direct POST /tools/call
        r3 = self._http.post(
            "/tools/call",
            headers=self._headers(extra_headers),
            json={"name": name, "arguments": args},
        )
        r3.raise_for_status()
        data = r3.json()
        if isinstance(data, dict):
            return data
        return {"ok": True, "result": data}

    # --- rpc result helper --------------------------------------------------------------------

    def _rpc_result(self, response: httpx.Response, rpc_id: str) -> McpCallResult:
        payload, session_id = self._extract_payload_and_session(response)
        if session_id:
            self._session_id = session_id

        if isinstance(payload, dict):
            if "error" in payload and payload["error"]:
                err = payload["error"]
                code = err.get("code", 500)
                status = response.status_code
                msg = err.get("message", "")
                if status in (401, 403):
                    raise McpUnauthorized(msg or str(err))
                if status == 400:
                    raise McpBadRequest(msg or str(err))
                if code == -32602:
                    raise McpInvalidParams(msg or str(err))
                raise McpError(msg or str(err))
            return McpCallResult(ok=True, id=rpc_id, result=payload.get("result"), status_code=response.status_code)

        return McpCallResult(ok=True, id=rpc_id, result=payload, status_code=response.status_code)

    # --- convenience --------------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
