from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any, Literal, List

from src.core.logging_setup import configure_logging
from src.core.settings import get_settings
from src.services.mcp.client import McpClient
from src.services.storage.helpers import get_mongodb_uri
from src.services.authentication.authenticator import Authenticator
from src.services.streaming.stream_variants import mcp_tool_to_openai_function

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
        self._lock = asyncio.Lock()
        self.log = logger or DEFAULT_LOGGER

        self._servers = servers
        self._server_urls = server_urls
        self._default_headers =  {t:default_headers or {} for t in self._servers}

        self._clients: Optional[Dict[Target,McpClient]] = {t:None for t in self._servers}

        # Cache of MCP tool descriptors and OpenAI tool schemas
        self._tools_by_target: Dict[Target, List[Dict[str, Any]]] = {t:[] for t in self._servers}
        self._openai_tools_cache: Optional[List[Dict[str, Any]]] = None
        
    # ────────── lifecycle ──────────

    async def close(self):
        async with self._lock:
            clients = [self._clients.get(s) for s in self._servers if self._clients.get(s)]
            for s in self._servers:
                self._clients[s] = None

        for client in clients:
            try:
                await client.close()
            except Exception:
                self.log.exception("Failed to close MCP client")
                
    # ────────── internal clients ──────────

    async def _build_client(self, target: Target) -> McpClient:
        client = self._clients.get(target)
        if client is None:
            client = McpClient(
                self._server_urls.get(target),
                default_headers=self._default_headers.get(target),
                logger=self.log,
            )
            self._clients[target] = client
        return client

    # ────────── initialization / discovery ──────────

    async def initialize(self, headers:Optional[dict]=None) -> None:
        """
        Eagerly connect to MCP servers and discover tools so the LLM can be given
        the function schemas before first token is generated.
        Idempotent; safe to call multiple times.
        """
        try:
            async with self._lock:
                if headers:
                    for s in self._servers:
                        self._default_headers[s].update(headers.get(s, {}))

                for s in self._servers:
                    await self._build_client(s)

            discovered_by_target: Dict[Target, List[Dict[str, Any]]] = {t: [] for t in self._servers}

            for s in self._servers:
                try:
                    discovered_by_target[s] = await self._discover_tools(s)
                except Exception as e:
                    self.log.warning("MCP tool discovery failed for %s: %s", s, e, exc_info=True)

            merged: List[Dict[str, Any]] = []
            async with self._lock:
                for s in self._servers:
                    self._tools_by_target[s] = discovered_by_target[s]
                    for t in self._tools_by_target[s]:
                        merged.append(mcp_tool_to_openai_function(t))
                self._openai_tools_cache = merged

            self.log.info(
                f"MCP initialized. Tools discovered: total:{len(merged)} "
                + " ".join([s + ':' + str(len(self._tools_by_target[s])) for s in self._servers])
            )
        except Exception as e:
            self.log.warning(
                "MCP manager initialization failed (tools may be unavailable): %s",
                e,
                exc_info=True,
            )


    async def _discover_tools(self, target: Target) -> None:
        """
        Ask the MCP server for available tools.
        Result shape is normalized to: [{"name":..., "description":..., "input_schema":{...}}, ...]
        """
        async with self._lock:
            cli = self._clients.get(target)

        if cli is None:
            raise RuntimeError(f"MCP client not initialized for target={target}")

        tools: List[Dict[str, Any]] = []

        res = await cli.tools_list_rpc()
        if res.ok and isinstance(res.result, dict):
            items = res.result.get("tools") or res.result.get("items") or res.result
            if isinstance(items, list):
                tools = items

        if not tools:
            raise RuntimeError(f"No tools discovered from MCP target={target}")

        normalized: List[Dict[str, Any]] = []
        for tool in tools:
            name = tool.get("name") or tool.get("tool_name") or ""
            desc = tool.get("description") or ""
            schema = tool.get("input_schema") or tool.get("parameters") or {}
            normalized.append({"name": name, "description": desc, "input_schema": schema})

        return normalized
    

    async def get_server_from_tool(self, tool_name: str) -> Optional[Target]:
        async with self._lock:
            for tgt in self._servers:
                for t in self._tools_by_target[tgt]:
                    if t.get("name") == tool_name:
                        return tgt
        return None

    # ────────── tool export to LLM ──────────

    async def openai_tools(self) -> List[Dict[str, Any]]:
        """
        Return cached OpenAI-style tool schemas. Empty list if discovery failed.
        """
        async with self._lock:
            if self._openai_tools_cache is None:
                merged: List[Dict[str, Any]] = []
                for tgt in self._servers:
                    for t in self._tools_by_target[tgt]:
                        merged.append(mcp_tool_to_openai_function(t))
                self._openai_tools_cache = merged
            return list(self._openai_tools_cache)

    # ────────── calling tools ──────────

    async def call_tool(
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
        async with self._lock:
            if target in self._servers:
                client = self._clients.get(target)
                if client is None:
                    raise RuntimeError(f"MCP client not initialized for target={target}")
                return await client.call_tool(name=name, args=arguments, extra_headers=extra_headers)

            clients = [(tgt, self._clients.get(tgt)) for tgt in self._servers]

        for tgt, client in clients:
            if client is None:
                continue
            try:
                return await client.call_tool(name=name, args=arguments, extra_headers=extra_headers)
            except Exception as e:
                self.log.debug("tool %s failed on %s: %s", name, tgt, e)

        raise RuntimeError(f"Tool invocation failed on all targets: {name}")
    

    async def cancel_tool_call(self, tool_name: str, reason: str | None = None) -> None:
        client_name = self.get_server_from_tool(tool_name=tool_name)
        await self._clients.get(client_name).cancel_request(reason)

# ──────────────────── Helper functions ──────────────────────────────

async def get_mcp_headers(auth: Authenticator, cache: str, logger=None) -> Dict[str, str]:
    log = logger or DEFAULT_LOGGER
    mongodb_uri = await get_mongodb_uri(auth.vault_url) if not settings.DEV else settings.MONGODB_URI_DEV
    
    headers = {
        "rag": {
            "mongodb-uri":  mongodb_uri,
        },
        "code": {
            "working-dir": str(cache),
        },
    }
    return headers
