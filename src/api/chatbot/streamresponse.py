from __future__ import annotations

import json
import logging
import os
import base64
from typing import Optional, List
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from starlette.responses import StreamingResponse
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY, HTTP_503_SERVICE_UNAVAILABLE

from src.core.logging_setup import configure_logging
from src.core.auth import AuthRequired, get_mongodb_uri
from src.core.available_chatbots import default_chatbot
from src.services.streaming.stream_variants import StreamVariant, from_sv_to_json, from_json_to_sv, CODE, IMAGE
from src.services.streaming.stream_orchestrator import run_stream
from src.services.mcp.mcp_manager import McpManager

from src.services.storage.thread_storage import recursively_create_dir_at_rw_dir
from src.services.storage import mongodb_storage

router = APIRouter()
log = logging.getLogger(__name__)
configure_logging()


def _sse_data(obj: dict) -> bytes:
    if obj.get("variant") == CODE:
        obj["content"] = [json.loads(obj["content"][0])["code"], obj["content"][1]]
    if obj.get("variant") == IMAGE:

        image_b64 = obj["content"]
        log.info(f"Base64 length (chars): {len(image_b64)}")
        decoded = base64.b64decode(image_b64)
        log.info(f"Decoded byte length: {len(decoded)}")
        
    payload = json.dumps(obj)
    return f"{payload}\n".encode("utf-8")

def verify_access_to_file(file_path):
    try:
        with open(file_path) as f:
            s = f.read()
    except:
        log.warning(f"The User requested a stream with a file path that cannot be accessed. Path: {file_path}\n"
                    "Note that if it is freva-config path, any usage of the freva library will fail.")


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

    database = await mongodb_storage.get_database(vault_url)
    
    mcp_mgr: McpManager = getattr(request.app.state, "mcp", None)
    mongodb_uri = await get_mongodb_uri(vault_url)
    auth_header = request.headers.get("Authorization") or request.headers.get("x-freva-user-token")
    
    freva_cfg_path = request.headers.get("freva-config") or request.headers.get("x-freva-config-path")
    if not freva_cfg_path:
        log.warning("The User requested a stream without a freva_config path being set. Thread ID: {}", thread_id)
    freva_cfg_path = "/work/ch1187/clint/nextgems/freva/evaluation_system.conf"
    verify_access_to_file(freva_cfg_path)
    
    headers = {
        "rag": {
            "mongodb-uri":  mongodb_uri,
            "Authorization": auth_header,
            },
        "code": {
            "Authorization": auth_header,
            "freva-config-path": freva_cfg_path,
            },
            }
    
    try:
        mcp_mgr.initialize(headers)
    except Exception as e:
        # Non-fatal: we can still run without tools; LLM just won't emit tool_calls.
        log.warning("MCP manager initialization failed (tools may be unavailable): %s", e)

    async def event_stream():
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id,
            user_id=user_id,
            user_input=user_input,
            database=database,
            mcp=mcp_mgr,          # reuses the app's global MCP manager
        ):
            yield _sse_data(from_sv_to_json(variant))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
