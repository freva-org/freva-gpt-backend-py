import os
import threading
from typing import Optional, Dict, Any, Literal

from src.services.mcp.client import McpClient, McpCallResult


Target = Literal["rag", "code"]


class McpManager:
    """
    Keeps one McpClient per target (rag / code), initializes lazily,
    and caches an Mcp-Session-Id per logical conversation/session key.
    Thread-safe for simple web workloads.
    """
    def __init__(
        self,
        rag_url: str,
        code_url: str,
        *,
        default_rag_headers: Optional[Dict[str, str]] = None,
        default_code_headers: Optional[Dict[str, str]] = None,
    ):
        self._rag_url = rag_url.rstrip("/")
        self._code_url = code_url.rstrip("/")

        self._rag_client: Optional[McpClient] = None
        self._code_client: Optional[McpClient] = None

        self._rag_defaults = default_rag_headers or {}
        self._code_defaults = default_code_headers or {}

        # session cache: (target, session_key) -> session_id
        self._sid: Dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # ---------- lifecycle ----------

    def close(self):
        with self._lock:
            if self._rag_client:
                self._rag_client.close()
                self._rag_client = None
            if self._code_client:
                self._code_client.close()
                self._code_client = None
            self._sid.clear()

    # ---------- internal ----------

    def _client(self, target: Target) -> McpClient:
        with self._lock:
            if target == "rag":
                if not self._rag_client:
                    self._rag_client = McpClient(self._rag_url, default_headers=self._rag_defaults)
                return self._rag_client
            else:
                if not self._code_client:
                    self._code_client = McpClient(self._code_url, default_headers=self._code_defaults)
                return self._code_client

    def _get_sid(self, target: Target, session_key: str) -> Optional[str]:
        with self._lock:
            return self._sid.get((target, session_key))

    def _set_sid(self, target: Target, session_key: str, sid: str):
        with self._lock:
            self._sid[(target, session_key)] = sid

    # ---------- public API ----------

    def ensure_initialized(
        self,
        target: Target,
        *,
        session_key: str,
        extra_headers: Optional[Dict[str, str]] = None,
        client_name: str = "backend",
        client_version: str = "dev",
    ) -> str:
        """
        Initialize the target server if we don't yet have a session id cached for this session_key.
        Returns the session id (or 'no-session-id' if the server doesn't set one).
        """
        if self._get_sid(target, session_key):
            return self._get_sid(target, session_key)  # type: ignore

        cli = self._client(target)
        sid = cli.initialize(client_name=client_name, client_version=client_version, extra_headers=extra_headers)
        self._set_sid(target, session_key, sid)
        return sid

    def call_tool(
        self,
        target: Target,
        *,
        session_key: str,
        name: str,
        arguments: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> McpCallResult:
        """
        Ensures initialize() once per (target, session_key), then calls the tool.
        """
        self.ensure_initialized(target, session_key=session_key, extra_headers=extra_headers)
        cli = self._client(target)
        # We still pass 'extra_headers' because the RAG header gate currently checks every request.
        return cli.call_tool(name, arguments, extra_headers=extra_headers)

def build_mcp_manager() -> McpManager:
    rag_url  = os.getenv("RAG_SERVER_URL",  "http://rag:8050/mcp")
    code_url = os.getenv("CODE_SERVER_URL", "http://code:8051/mcp")

    # Default headers that are always OK to send.
    # RAG still needs vault/rest on every call for now.
    rag_defaults = {}
    code_defaults = {}

    return McpManager(
        rag_url=rag_url,
        code_url=code_url,
        default_rag_headers=rag_defaults,
        default_code_headers=code_defaults,
    )