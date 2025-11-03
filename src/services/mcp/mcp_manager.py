from __future__ import annotations

import os
import threading
import logging
from typing import Optional, Dict, Any, Literal, List, Tuple

from src.core.logging_setup import configure_logging
from src.services.mcp.client import McpClient, McpCallResult

log = logging.getLogger(__name__)
configure_logging()

Target = Literal["rag", "code"]


def _to_openai_function(tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert an MCP tool descriptor to OpenAI-style tool schema:
    MCP (typical):
      {"name": "search", "description": "...", "input_schema": {...}}
    OpenAI tool:
      {"type":"function","function":{"name":"search","description":"...","parameters":{...}}}
    Be permissive: fall back to {} if schema missing.
    """
    name = tool.get("name") or ""
    desc = tool.get("description") or ""
    params = tool.get("input_schema") or tool.get("parameters") or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params if isinstance(params, dict) else {},
        },
    }


class McpManager:
    """
    Keeps one McpClient per target (rag / code), initializes lazily,
    caches an MCP session id per logical conversation (handled by McpClient),
    and caches discovered tool schemas for export to LLM.

    Thread-safe for simple web workloads (single-process).
    """

    def __init__(
        self,
        *,
        rag_url: str,
        code_url: str,
        default_rag_headers: Optional[Dict[str, str]] = None,
        default_code_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._lock = threading.RLock()

        self._rag_url = rag_url
        self._code_url = code_url
        self._rag_defaults = default_rag_headers or {}
        self._code_defaults = default_code_headers or {}

        self._rag_client: Optional[McpClient] = None
        self._code_client: Optional[McpClient] = None

        # Cache of MCP tool descriptors and OpenAI tool schemas
        self._tools_by_target: Dict[Target, List[Dict[str, Any]]] = {"rag": [], "code": []}
        self._openai_tools_cache: Optional[List[Dict[str, Any]]] = None
        
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

    # ---------- internal clients ----------

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

    # ---------- initialization / discovery ----------

    def initialize(self, headers:Optional[dict]=None) -> None:
        """
        Eagerly connect to MCP servers and discover tools so the LLM can be given
        the function schemas before first token is generated.
        Idempotent; safe to call multiple times.
        """
        if headers:
            self._rag_defaults.update(headers["rag"])
            self._code_defaults.update(headers["code"])
        with self._lock:
            # create clients if needed
            _ = self._client("rag")
            _ = self._client("code")

            # probe both, but tolerate failures (log + continue)
            for tgt in ("rag", "code"):
                try:
                    self._discover_tools(tgt)  # populates _tools_by_target[tgt]
                except Exception as e:
                    log.warning("MCP tool discovery failed for %s: %s", tgt, e, exc_info=True)

            # build OpenAI tool list (merged)
            self._openai_tools_cache = []
            for tgt in ("rag", "code"):
                for t in self._tools_by_target[tgt]:  # type: ignore[index]
                    self._openai_tools_cache.append(_to_openai_function(t))

            log.info(
                "MCP initialized. Tools discovered: rag=%d code=%d total=%d",
                len(self._tools_by_target["rag"]),
                len(self._tools_by_target["code"]),
                len(self._openai_tools_cache),
            )

    def _discover_tools(self, target: Target) -> None:
        """
        Ask the MCP server for available tools. We try a few strategies to be resilient
        across server implementations:
          1) JSON-RPC method 'tools/list'
          2) HTTP GET /tools
          3) JSON-RPC method 'tools.list'
        Result shape is normalized to: [{"name":..., "description":..., "input_schema":{...}}, ...]
        """
        cli = self._client(target)
        tools: List[Dict[str, Any]] = []

        # strategy 1: JSON-RPC tools/list
        try:
            res = cli.tools_list_rpc()
            if res.ok and isinstance(res.result, dict):
                items = res.result.get("tools") or res.result.get("items") or res.result
                if isinstance(items, list):
                    tools = items  # assume already normalized
        except Exception:
            pass

        # strategy 2: GET /tools
        if not tools:
            try:
                items = cli.tools_list_http()
                if isinstance(items, list):
                    tools = items
            except Exception:
                pass

        # strategy 3: JSON-RPC tools.list
        if not tools:
            try:
                res = cli.tools_list_rpc(dot_name=True)
                if res.ok and isinstance(res.result, dict):
                    items = res.result.get("tools") or res.result.get("items") or res.result
                    if isinstance(items, list):
                        tools = items
            except Exception:
                pass

        if not tools:
            raise RuntimeError(f"No tools discovered from MCP target={target}")

        # Normalize & cache
        normalized: List[Dict[str, Any]] = []
        for tool in tools:
            name = tool.get("name") or tool.get("tool_name") or ""
            desc = tool.get("description") or ""
            schema = tool.get("input_schema") or tool.get("parameters") or {}
            normalized.append({"name": name, "description": desc, "input_schema": schema})

        with self._lock:
            self._tools_by_target[target] = normalized
            # invalidate merged cache
            self._openai_tools_cache = None

    # ---------- tool export to LLM ----------

    def openai_tools(self) -> List[Dict[str, Any]]:
        """
        Return cached OpenAI-style tool schemas. Empty list if discovery failed.
        """
        with self._lock:
            if self._openai_tools_cache is None:
                # rebuild merged cache on-demand
                merged: List[Dict[str, Any]] = []
                for tgt in ("rag", "code"):
                    for t in self._tools_by_target[tgt]:
                        merged.append(_to_openai_function(t))
                self._openai_tools_cache = merged
            return list(self._openai_tools_cache)

    # ---------- calling tools ----------

    def call_tool(
        self,
        target: Target | str,
        *,
        session_key: str,
        name: str,
        arguments: Dict[str, Any],
        extra_headers: Optional[Dict]=None,
    ) -> Dict[str, Any]:
        """
        Call a tool on the chosen target. If 'target' isn't 'rag' or 'code',
        try 'rag' first then 'code'.
        """
        if target in ("rag", "code"):
            return self._client(target).call_tool(name=name, args=arguments, session_key=session_key, extra_headers=extra_headers)

        # fallback routing: best-effort
        for tgt in ("rag", "code"):
            try:
                return self._client(tgt).call_tool(name=name, args=arguments, session_key=session_key, extra_headers=extra_headers)
            except Exception as e:
                log.debug("tool %s failed on %s: %s", name, tgt, e)
        raise RuntimeError(f"Tool invocation failed on all targets: {name}")


def build_mcp_manager() -> McpManager:
    """
    Build and eagerly initialize a manager so tools are ready for prompting.
    """
    rag_url = os.getenv("RAG_SERVER_URL", "http://rag:8050")
    code_url = os.getenv("CODE_SERVER_URL", "http://code:8051")

    # Defaults to send; per-call headers (vault/rest) are added at call time.
    rag_defaults: Dict[str, str] = {}
    code_defaults: Dict[str, str] = {} 

    mgr = McpManager(
        rag_url=rag_url,
        code_url=code_url,
        default_rag_headers=rag_defaults,
        default_code_headers=code_defaults,
    )
    # try:
    #     mgr.initialize()
    # except Exception as e:
    #     # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
    #     log.warning("MCP manager initialization failed (tools may be unavailable): %s", e, exc_info=True)
    return mgr