from __future__ import annotations

import json
import logging
import os
import base64
import time
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException, Depends
from starlette.responses import StreamingResponse
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY, HTTP_503_SERVICE_UNAVAILABLE

from freva_gpt.core.available_chatbots import default_chatbot
from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.core.prompting import get_entire_prompt
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)
from freva_gpt.services.streaming.active_conversations import (
    ConversationState,
    add_to_conversation,
    cancel_tool_tasks,
    check_thread_exists,
    end_and_save_conversation,
    get_conversation_state,
    new_thread_id,
)
from freva_gpt.services.streaming.helpers import chunks
from freva_gpt.services.streaming.stream_orchestrator import (
    prepare_for_stream,
    run_stream,
)
from freva_gpt.services.streaming.stream_variants import (
    IMAGE,
    SVStreamEnd,
    from_sv_to_json,
)

router = APIRouter()

log = logging.getLogger(__name__)
configure_logging()

CHECK_INTERVAL = 3  # seconds, the interval to wait before check STOP request


def _sse_data(obj: dict):
    if obj.get("variant") == IMAGE:
        image_b64 = obj.get("content")
        id = obj.get("id")
        CHUNK_SIZE = 16_384  # 16 KiB per JSON line

        for frag in chunks(image_b64, CHUNK_SIZE):
            payload = json.dumps({"variant":"Image", "content":frag, "id":id})
            yield f"{payload}\n".encode("utf-8")
    else:
        payload = json.dumps(obj)
        yield f"{payload}\n".encode("utf-8")



@router.get("/streamresponse", dependencies=[AuthRequired])
async def streamresponse(
    thread_id: Optional[str] = Query(None),
    input: Optional[str] = Query(None),
    chatbot: Optional[str] = Query(None),
    Auth: Authenticator = Depends(auth_dependency),
):
    """
    Thin HTTP wrapper that delegates streaming to the orchestrator.
    """
    read_history = False
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

    user_name = Auth.username

    if not Auth.vault_url:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )
    
    # Get thread storage
    Storage = await get_thread_storage(vault_url=Auth.vault_url, user_name=user_name, thread_id=thread_id)

    system_prompt = get_entire_prompt(user_name, thread_id, model_name)

    async def event_stream():
        prep_error = await prepare_for_stream(
            thread_id=thread_id,
            user_id=user_name,
            Auth=Auth,
            Storage=Storage,
            read_history=read_history,
        )
        if prep_error:
            yield _sse_data(from_sv_to_json(prep_error))
            return

        last_check = time.monotonic()
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id,
            user_input=user_input,
            system_prompt=system_prompt,
        ):
            for data in _sse_data(from_sv_to_json(variant)):
                yield data
            now = time.monotonic()
            # Check if there is STOP request from
            if now - last_check > CHECK_INTERVAL:
                last_check = now
                state = await get_conversation_state(thread_id)
                if state == ConversationState.STOPPING:
                    end_v = SVStreamEnd(message="Stream is stopped by user.")
                    yield _sse_data(from_sv_to_json(end_v))
                    await add_to_conversation(thread_id, [end_v])
                    await cancel_tool_tasks(thread_id)
                    await end_and_save_conversation(thread_id, Storage)
                    return
        await end_and_save_conversation(thread_id, Storage)

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )
