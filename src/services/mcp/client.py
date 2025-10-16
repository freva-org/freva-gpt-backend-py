import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx


# ---- public dataclass you can log/inspect -------------------------------------------------------

@dataclass
class McpCallResult:
    ok: bool
    id: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    status_code: Optional[int] = None
    raw: Optional[str] = None
    duration_ms: int = 0


# ---- errors --------------------------------------------------------------------------------------

class McpError(Exception):
    pass

class McpInvalidParams(McpError):
    pass

class McpUnauthorized(McpError):
    pass

class McpBadRequest(McpError):
    pass


# ---- core client --------------------------------------------------------------------------------

class McpClient:
    """
    Minimal JSON-RPC over Streamable HTTP (SSE) client with:
      - initialize() that returns & caches Mcp-Session-Id
      - tools call with method fallback: tools/call → tools.call → tools.invoke
      - automatic dual Accept header
      - per-server default headers + per-call extra headers
    """

    def __init__(
        self,
        base_url: str,  # e.g. http://rag:8050/mcp
        *,
        default_headers: Optional[Dict[str, str]] = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_headers = default_headers or {}
        self._session_id: Optional[str] = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout),
            transport=httpx.HTTPTransport(retries=0),
        )

    # --- headers ----------------------------------------------------------------

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            # IMPORTANT: dual accept is required by FastMCP streamable http
            "Accept": "application/json, text/event-stream",
        }
        h.update(self.default_headers)
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
            h["Mcp-Protocol-Version"] = "2025-03-26"
        if extra:
            h.update(extra)
        return h

    # --- sse response parsing ---------------------------------------------------

    @staticmethod
    def _parse_sse_text(s: str) -> Dict[str, Any]:
        """
        Fast path: server usually returns a single 'event: message' with one 'data: {...}'.
        We scan lines and parse the last data: payload.
        """
        data_line = None
        for line in s.splitlines():
            if line.startswith("data: "):
                data_line = line[6:]
        if not data_line:
            raise McpError(f"Missing SSE 'data:' line in response: {s[:200]}")
        try:
            return json.loads(data_line)
        except json.JSONDecodeError:
            # sometimes server returns plain body without SSE prefix
            try:
                return json.loads(s)
            except Exception:
                raise McpError(f"Failed to parse server response: {s[:300]}")

    # --- initialize -------------------------------------------------------------

    def initialize(self, client_name: str = "backend", client_version: str = "1.0",
                   extra_headers: Optional[Dict[str, str]] = None) -> str:
        """
        Performs JSON-RPC initialize and captures Mcp-Session-Id from response headers.
        Returns the session id (also cached on the client).
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": client_version},
            },
        }
        t0 = time.time()
        r = self._http.post(self.base_url, headers=self._headers(extra_headers), json=payload)
        dur = int((time.time() - t0) * 1000)

        # FastMCP puts session in header
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if not sid:
            # some builds don’t echo it; still OK to proceed
            pass
        else:
            self._session_id = sid

        body = r.text
        obj = self._parse_sse_text(body)

        if "error" in obj:
            err = obj["error"]
            raise McpBadRequest(f"initialize failed: {err.get('message')} ({err})")

        return self._session_id or "no-session-id"

    # --- tools call with method fallback ---------------------------------------

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
        *,
        request_id: Optional[str] = None,
    ) -> McpCallResult:
        """
        Call a tool with graceful fallback across method name variants:
          tools/call → tools.call → tools.invoke
        """
        rid = request_id or uuid.uuid4().hex[:8]
        methods = ["tools/call", "tools.call", "tools.invoke"]

        last_err: Optional[Dict[str, Any]] = None
        t0 = time.time()
        for m in methods:
            payload = {"jsonrpc": "2.0", "id": rid, "method": m,
                       "params": {"name": name, "arguments": arguments}}

            r = self._http.post(self.base_url, headers=self._headers(extra_headers), json=payload)
            raw = r.text
            obj = self._parse_sse_text(raw)
            status = r.status_code

            if "error" not in obj:
                return McpCallResult(
                    ok=True, id=str(rid), result=obj.get("result"), status_code=status,
                    raw=raw, duration_ms=int((time.time() - t0) * 1000)
                )

            # stash and see if this is the classic -32602 mismatch
            last_err = obj["error"]
            if last_err.get("code") == -32602:
                # try next method name
                continue
            # other errors → map and stop
            code = last_err.get("code")
            msg = last_err.get("message", "")
            if status in (401, 403) or "Unauthorized" in msg:
                raise McpUnauthorized(msg or str(last_err))
            if status == 400:
                raise McpBadRequest(msg or str(last_err))
            raise McpError(f"MCP error {code}: {msg or last_err}")

        # all fallbacks exhausted
        raise McpInvalidParams(f"All method variants failed with -32602. Last error: {last_err}")

    # --- convenience ------------------------------------------------------------

    def close(self):
        self._http.close()
