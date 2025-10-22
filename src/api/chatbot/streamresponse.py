from __future__ import annotations

import json
import logging
import os
from typing import Optional, List

from fastapi import APIRouter, Request, Query, HTTPException
from starlette.responses import StreamingResponse

from src.core.auth import AuthRequired
from src.core.available_chatbots import default_chatbot
from src.services.streaming.stream_variants import StreamVariant, to_wire_dict
from src.services.streaming.stream_orchestrator import run_stream
from src.services.mcp.mcp_manager import McpManager

# storage backends
from src.services.storage.thread_storage import (
    append_thread as disk_append_thread,
    read_thread as disk_read_thread,
    recursively_create_dir_at_rw_dir,
)
from src.services.storage.mongodb_storage import get_database
from src.services.storage import router as storage_router  # optional Mongo wrapper

router = APIRouter()
log = logging.getLogger(__name__)


def _sse_data(obj: dict) -> bytes:
    payload = json.dumps(obj, ensure_ascii=False)
    return f"{payload}\n\n".encode("utf-8")


@router.get("/streamresponse", dependencies=[AuthRequired])
async def streamresponse(
    request: Request,
    thread_id: Optional[str] = Query(None),
    user_input: Optional[str] = Query(None),
    input: Optional[str] = Query(None),
    chatbot: Optional[str] = Query(None),
):
    """
    Thin HTTP wrapper that selects storage (Mongo vs Disk) and delegates streaming to the orchestrator.
    The orchestrator ensures MCP session key == thread_id for per-conversation isolation.
    """
    ui = user_input if user_input is not None else input
    if ui is None:
        raise HTTPException(status_code=422, detail="Missing user input")

    model_name = chatbot or default_chatbot()
    user_id = getattr(request.state, "username", "anonymous")
    recursively_create_dir_at_rw_dir(user_id, thread_id or "tmp")

    # Storage decision:
    vault_url = request.headers.get("x-freva-vault-url")
    prefer_disk_env = os.getenv("FREVA_STORAGE", "").lower() == "disk"

    use_mongo = bool(vault_url) and not prefer_disk_env
    database = None
    if use_mongo:
        try:
            database = await get_database(vault_url)
        except Exception as e:
            log.warning("streamresponse: Mongo unavailable, falling back to Disk: %s", e)
            use_mongo = False

    # Persist hook (async)
    async def persist(variants: List[StreamVariant]) -> None:
        if use_mongo and database is not None:
            await storage_router.append_thread(thread_id_val, user_id, variants, database)  # type: ignore
        else:
            disk_append_thread(thread_id_val, variants, ensure_end=False)

    # Load hook (sync)
    def load_thread(thread_id_in: str) -> List[StreamVariant]:
        if use_mongo and database is not None:
            # If you need Mongo continuation, prefetch prior SVs here (async) before run_stream
            # and close over them instead. For now, use disk for continuation.
            raise RuntimeError("Reading prior thread from Mongo in sync context is not supported here.")
        return disk_read_thread(thread_id_in)

    thread_id_val = thread_id or None
    mcp_mgr: McpManager = getattr(request.app.state, "mcp", None)

    async def event_stream():
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id_val,
            user_id=user_id,
            user_input=ui,
            request=request,
            mcp=mcp_mgr,              # reuses the app's global MCP manager
            persist=persist,
            load_thread=load_thread,
        ):
            yield _sse_data(to_wire_dict(variant))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
