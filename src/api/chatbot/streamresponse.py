from __future__ import annotations

import json
import logging
import os
import base64
import time
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException
from starlette.responses import StreamingResponse
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY, HTTP_503_SERVICE_UNAVAILABLE

from src.core.logging_setup import configure_logging
from src.core.auth import AuthRequired
from src.core.available_chatbots import default_chatbot
from src.core.prompting import get_entire_prompt
from src.services.streaming.stream_variants import SVStreamEnd, SVServerError, from_sv_to_json, CODE, IMAGE
from src.services.streaming.stream_orchestrator import run_stream, get_conversation_history
from src.services.streaming.helpers import get_mcp_headers_from_req
from src.services.streaming.active_conversations import (
    Registry, RegistryLock,
    ConversationState, get_conversation_state, 
    end_and_save_conversation, add_to_conversation,
    new_thread_id, initialize_conversation, check_thread_exists,
)

from src.services.storage import mongodb_storage

router = APIRouter()
log = logging.getLogger(__name__)
configure_logging()

CHECK_INTERVAL = 3  # seconds, the interval to wait before check STOP request


def _sse_data(obj: dict) -> bytes:
    if obj.get("variant") == CODE:
        obj["content"] = [json.loads(obj["content"][0])["code"], obj["content"][1]]
    if obj.get("variant") == IMAGE:
        # DEBUG to verify image payload that is sent from backend
        image_b64 = obj["content"]
        log.info(f"Base64 length (chars): {len(image_b64)}")
        decoded = base64.b64decode(image_b64)
        log.info(f"Decoded byte length: {len(decoded)}")
        
    payload = json.dumps(obj)
    return f"{payload}\n".encode("utf-8")


@router.get("/streamresponse", dependencies=[AuthRequired])
async def streamresponse(
    request: Request,
    thread_id: Optional[str] = Query(None),
    input: Optional[str] = Query(None),
    chatbot: Optional[str] = Query(None),
):
    """
    Thin HTTP wrapper that delegates streaming to the orchestrator.
    """
    read_history=False
    if not thread_id:
        thread_id = await new_thread_id()
    else:
        if not await check_thread_exists(thread_id):
            read_history = True

    user_input = input or None
    if user_input is None:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY, 
            detail="Input not found. Please provide a non-empty input in the query parameters or the headers, of type String."
            )

    model_name = chatbot or default_chatbot()

    user_id = getattr(request.state, "username", "anonymous")

    vault_url = request.headers.get("x-freva-vault-url")
    if not vault_url:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )
    
    database = await mongodb_storage.get_database(vault_url)

    system_prompt = get_entire_prompt(user_id, thread_id, model_name)

    async def event_stream():
        messages: List[Dict[str, Any]] = []
        if read_history:
            try:
                messages = await get_conversation_history(thread_id, database)
            except Exception as e:
                msg = f"Reading conversation history failed: {e}"
                log.exception(msg)
                err = SVServerError(message=msg)
                end = SVStreamEnd(message="Stream ended with an error.")
                await add_to_conversation(thread_id, [err, end])
                await end_and_save_conversation(thread_id)
                yield err
                yield end
                return

        # Check if the conversation already exists in registry
        # If not initialize it, and add the first messages 
        await initialize_conversation(thread_id, user_id, request=request, messages=messages)

        last_check = time.monotonic()
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id,
            user_id=user_id,
            user_input=user_input,
            system_prompt=system_prompt,
            database=database,
        ):
            yield _sse_data(from_sv_to_json(variant))
            now = time.monotonic()
            # Check if there is STOP request from
            if now-last_check > CHECK_INTERVAL:
                last_check = now
                state = await get_conversation_state(thread_id)
                if state == ConversationState.STOPPING:
                    end_v = SVStreamEnd(message="Stream is stopped by user.")
                    yield _sse_data(from_sv_to_json(end_v))
                    await add_to_conversation(thread_id, [end_v])
                    await end_and_save_conversation(thread_id, database)
                    return
        await end_and_save_conversation(thread_id, database)
    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform", 
            },
    )
