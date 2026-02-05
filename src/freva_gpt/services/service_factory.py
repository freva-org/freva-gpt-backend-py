from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, Request

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.settings import get_server_url_dict, get_settings

from .authentication.authenticator import Authenticator
from .authentication.dev_auth import DevAuthenticator
from .authentication.full_auth import FullAuthenticator
from .mcp.mcp_manager import McpManager, get_mcp_headers
from .storage.helpers import create_dir_at_cache
from .storage.mongodb_storage import ThreadStorage

DEFAULT_LOGGER = configure_logging(__name__)

settings = get_settings()
CACHE_ROOT = Path("./cache")


def get_authenticator() -> Authenticator:
    if settings.DEV:
        return DevAuthenticator  # type:ignore
    return FullAuthenticator  # type:ignore


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
    auth: Authenticator = AuthCls(request)
    await auth.run()
    return auth


# Convenience alias for router-wide protection:
AuthRequired = Depends(auth_dependency)


async def get_thread_storage(
    vault_url: Optional[str] = None,
    user_name: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> ThreadStorage:
    if user_name and thread_id:
        create_dir_at_cache(user_name, thread_id)
    Storage: ThreadStorage = await ThreadStorage.create(vault_url=vault_url)
    return Storage


async def get_mcp_manager(
    authenticator: Authenticator, thread_id: str
) -> McpManager | None:
    """
    Build and eagerly initialize a manager so tools are ready for prompting.
    """
    logger = configure_logging(
        __name__, thread_id=thread_id, user_id=authenticator.username
    )

    MCP_SERVER_URLs = get_server_url_dict(settings.AVAILABLE_MCP_SERVERS)

    # Defaults to send; per-call headers (vault/rest) are added at call time.
    default_headers: Dict[str, str] = {}

    mgr = McpManager(
        servers=settings.AVAILABLE_MCP_SERVERS,
        server_urls=MCP_SERVER_URLs,
        default_headers=default_headers,
        logger=logger,
    )

    cache = CACHE_ROOT / thread_id

    extra_headers = await get_mcp_headers(authenticator, cache)

    try:
        mgr.initialize(extra_headers)
        logger.info("Successfully initialized the MCPManager!")
        return mgr
    except Exception as e:
        # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
        logger.warning(
            "MCP manager initialization failed (tools may be unavailable): %s",
            e,
        )
        return None


__all__ = ["Authenticator", "ThreadStorage", "McpManager"]
