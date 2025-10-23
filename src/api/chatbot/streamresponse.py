from __future__ import annotations

import json
import logging
import os
from typing import Optional, List

from fastapi import APIRouter, Request, Query, HTTPException
from starlette.responses import StreamingResponse
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY, HTTP_503_SERVICE_UNAVAILABLE

from src.core.auth import AuthRequired, get_mongodb_uri
from src.core.available_chatbots import default_chatbot
from src.services.streaming.stream_variants import StreamVariant, to_wire_dict
from src.services.streaming.stream_orchestrator import run_stream
from src.services.mcp.mcp_manager import McpManager

from src.services.storage.thread_storage import recursively_create_dir_at_rw_dir
from src.services.storage.mongodb_storage import get_database

router = APIRouter()
log = logging.getLogger(__name__)


def _sse_data(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False)
    return f"{payload}\n\n".encode("utf-8")


@router.get("/streamresponse", dependencies=[AuthRequired])
async def streamresponse(
    request: Request,
    thread_id: Optional[str] = Query(None),
    input: Optional[str] = Query(None),
    chatbot: Optional[str] = Query(None),
):
    """
    Thin HTTP wrapper that delegates streaming to the orchestrator.
    The orchestrator ensures MCP session key == thread_id for per-conversation isolation.
    """
    thread_id = thread_id or None

    user_input = input or None
    if user_input is None:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY, 
            detail="Input not found. Please provide a non-empty input in the query parameters or the headers, of type String."
            )

    model_name = chatbot or default_chatbot()

    user_id = getattr(request.state, "username", "anonymous")

    recursively_create_dir_at_rw_dir(user_id, thread_id or "tmp")

    vault_url = request.headers.get("x-freva-vault-url")
    if not vault_url:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )

    try:
        database = await get_database(vault_url)
    except Exception as e:
        log.error("stream: get_database_failed vault_url=%s err=%s", vault_url, e)
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to the database: {e}",
        )

    mcp_mgr: McpManager = getattr(request.app.state, "mcp", None)
    headers = {"rag": {"mongodb-uri": get_mongodb_uri(vault_url),
                       "Authentication": request.headers.get("Authentication")},
               "code": {"Authentication": request.headers.get("Authentication")},
               }
    mcp_mgr.initialize(headers)

    async def event_stream():
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id,
            user_id=user_id,
            user_input=user_input,
            database=database,
            mcp=mcp_mgr,          # reuses the app's global MCP manager
        ):
            yield _sse_data(to_wire_dict(variant))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
