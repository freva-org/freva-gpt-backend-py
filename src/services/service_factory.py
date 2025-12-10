from pathlib import Path
import logging
from typing import Optional, Dict
from fastapi import Depends, Request

from src.core.logging_setup import configure_logging
from src.core.settings import get_settings, get_server_url_dict

from .authentication.authenticator import Authenticator
from .authentication.dev_auth import DevAuthenticator
from .authentication.full_auth import FullAuthenticator

from .mcp.mcp_manager import McpManager, get_mcp_headers

from .storage.thread_storage import ThreadStorage, create_dir_at_cache
from .storage.mongodb_storage import MongoThreadStorage
from .storage.disk_storage import DiskThreadStorage

log = logging.getLogger(__name__)
configure_logging()

settings = get_settings()

CACHE_ROOT = Path("./cache")

def get_authenticator() -> Authenticator:
    if settings.DEV:
        return DevAuthenticator
    return FullAuthenticator

async def auth_dependency(
    request: Request,
) -> Authenticator:
    """
    FastAPI dependency:
    - builds the appropriate authenticator for this request
    - runs it
    - returns the authenticated object (or raises HTTPException)
    """
    AuthCls = get_authenticator()
    auth = AuthCls(request)
    await auth.run()
    return auth

# Convenience alias for router-wide protection:
AuthRequired = Depends(auth_dependency)


async def get_thread_storage(
    vault_url: Optional[str]= None,
    user_name: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> ThreadStorage:
    if user_name and thread_id:
        create_dir_at_cache(user_name, thread_id)
    if settings.DEV:
        # DEV mode: disk storage (no MongoDB dependency)
        return DiskThreadStorage()
    else:
        # PROD: MongoDB storage
        return await MongoThreadStorage.create(vault_url=vault_url)


async def get_mcp_manager(authenticator: Authenticator, thread_id: str) -> McpManager:
    """
    Build and eagerly initialize a manager so tools are ready for prompting.
    """
    MCP_SERVER_URLs = get_server_url_dict(settings.AVAILABLE_MCP_SERVERS)

    # Defaults to send; per-call headers (vault/rest) are added at call time.
    default_headers: Dict[str, str] = {}

    mgr = McpManager(
        servers=settings.AVAILABLE_MCP_SERVERS,
        server_urls=MCP_SERVER_URLs,
        default_headers=default_headers,
    )

    cache = CACHE_ROOT / thread_id

    extra_headers = await get_mcp_headers(authenticator, cache)

    try:
        mgr.initialize(extra_headers)
        return mgr
    except Exception as e:
        # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
        log.warning("MCP manager initialization failed (tools may be unavailable): %s", e)
