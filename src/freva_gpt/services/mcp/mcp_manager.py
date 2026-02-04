from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Literal, Optional

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.settings import get_settings
from freva_gpt.services.authentication.authenticator import Authenticator
from freva_gpt.services.mcp.client import McpClient
from freva_gpt.services.storage.helpers import get_mongodb_uri
from freva_gpt.services.streaming.stream_variants import (
    mcp_tool_to_openai_function,
)

settings = get_settings()
DEFAULT_LOGGER = configure_logging(__name__)

Target = Literal[*settings.AVAILABLE_MCP_SERVERS]


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
        servers: List,
        server_urls: Dict[Target, str],
        default_headers: Optional[Dict[str, str]] = None,
        logger=None,
    ) -> None:
        self._lock = threading.RLock()
        self.log = logger or DEFAULT_LOGGER

        self._servers = servers
        self._server_urls = server_urls
        self._default_headers = {t: default_headers or {} for t in self._servers}

        self._clients: Optional[Dict[Target, McpClient]] = {
            t: None for t in self._servers
        }

        # Cache of MCP tool descriptors and OpenAI tool schemas
        self._tools_by_target: Dict[Target, List[Dict[str, Any]]] = {
            t: [] for t in self._servers
        }
        self._openai_tools_cache: Optional[List[Dict[str, Any]]] = None

    # ────────── lifecycle ──────────

    def close(self):
        with self._lock:
            for s in self._servers:
                if self._clients.get(s):
                    self._clients.get(s).close()
                    self._clients.update({s: None})

    # ────────── internal clients ──────────

    def _build_client(self, target: Target) -> McpClient:
        with self._lock:
            if not self._clients.get(target):
                self._clients.update(
                    {
                        target: McpClient(
                            self._server_urls.get(target),
                            default_headers=self._default_headers.get(target),
                        )
                    }
                )

    # ────────── initialization / discovery ──────────

    def initialize(self, headers: Optional[dict] = None) -> None:
        """
        Eagerly connect to MCP servers and discover tools so the LLM can be given
        the function schemas before first token is generated.
        Idempotent; safe to call multiple times.
        """
        try:
            if headers:
                for s in self._servers:
                    self._default_headers[s].update(headers.get(s, {}))
            with self._lock:
                # create clients if needed
                for s in self._servers:
                    self._build_client(s)

                    # probe server, but tolerate failures (log + continue)
                    try:
                        self._discover_tools(s)  # populates _tools_by_target[tgt]
                    except Exception as e:
                        self.log.warning(
                            "MCP tool discovery failed for %s: %s",
                            s,
                            e,
                            exc_info=True,
                        )

                # build OpenAI tool list (merged)
                self._openai_tools_cache = []
                for tgt in self._servers:
                    for t in self._tools_by_target[tgt]:  # type: ignore[index]
                        self._openai_tools_cache.append(mcp_tool_to_openai_function(t))

                self.log.info(
                    f"MCP initialized. Tools discovered: total:{len(self._openai_tools_cache)} " + \
                    " ".join([s+':' + str(len(self._tools_by_target[s])) for s in self._servers])
                )
        except Exception as e:
            # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
            self.log.warning("MCP manager initialization failed (tools may be unavailable): %s", e, exc_info=True)


    def _discover_tools(self, target: Target) -> None:
        """
        Ask the MCP server for available tools.
        Result shape is normalized to: [{"name":..., "description":..., "input_schema":{...}}, ...]
        """
        cli = self._clients.get(target)
        tools: List[Dict[str, Any]] = []

        res = cli.tools_list_rpc()
        if res.ok and isinstance(res.result, dict):
            items = res.result.get("tools") or res.result.get("items") or res.result
            if isinstance(items, list):
                tools = items  # assume already normalized

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

    def get_server_from_tool(self, tool_name: str) -> Optional[Target]:
        """
        Given a tool name, return which server it belongs to,
        or None if not found.
        """
        with self._lock:
            for tgt in self._servers:
                for t in self._tools_by_target[tgt]:
                    if t.get("name") == tool_name:
                        return tgt
        return None

    # ────────── tool export to LLM ──────────

    def openai_tools(self) -> List[Dict[str, Any]]:
        """
        Return cached OpenAI-style tool schemas. Empty list if discovery failed.
        """
        with self._lock:
            if self._openai_tools_cache is None:
                # rebuild merged cache on-demand
                merged: List[Dict[str, Any]] = []
                for tgt in self._servers:
                    for t in self._tools_by_target[tgt]:
                        merged.append(mcp_tool_to_openai_function(t))
                self._openai_tools_cache = merged
            return list(self._openai_tools_cache)

    # ────────── calling tools ──────────

    def call_tool(
        self,
        target: Target | str,
        *,
        name: str,
        arguments: Dict[str, Any],
        extra_headers: Optional[Dict]=None,
    ) -> Dict[str, Any]:
        """
        Call a tool on the chosen target. If 'target' isn't in AVAILABLE_MCP_SERVERS, 
        all the available servers are called as best-effort.
        """
        if target in self._servers:
            return self._clients.get(target).call_tool(name=name, args=arguments, extra_headers=extra_headers)
        
        # fallback routing: best-effort
        for tgt in self._servers:
            try:
                return self._clients.get(tgt).call_tool(name=name, args=arguments, extra_headers=extra_headers)
            except Exception as e:
                self.log.debug("tool %s failed on %s: %s", name, tgt, e)
        raise RuntimeError(f"Tool invocation failed on all targets: {name}")


# ──────────────────── Helper functions ──────────────────────────────

async def get_mcp_headers(auth: Authenticator, cache: str, logger=None) -> Dict[str, str]:
    log = logger or DEFAULT_LOGGER
    mongodb_uri = await get_mongodb_uri(auth.vault_url) if not settings.DEV else settings.MONGODB_URI_DEV
    access_token = auth.access_token
    
    auth_header = f"Bearer {access_token}" if access_token else None
    
    headers = {
        "rag": {
            "Authorization": auth_header,
            "mongodb-uri":  mongodb_uri,
            },
        "code": {
            "Authorization": auth_header,
            "working-dir": str(cache),
            },
            }
    return headers
