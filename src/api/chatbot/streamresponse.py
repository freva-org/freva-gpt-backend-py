from __future__ import annotations

import json
import os
import base64
import time
from typing import Optional, List, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Request, Query, HTTPException, Depends
from starlette.responses import StreamingResponse

from src.core.logging_setup import configure_logging
from src.core.available_chatbots import default_chatbot
from src.core.prompting import get_entire_prompt

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage

from src.services.streaming.stream_variants import SVStreamEnd, from_sv_to_json, IMAGE
from src.services.streaming.stream_orchestrator import run_stream, prepare_for_stream
from src.services.streaming.helpers import chunks
from src.services.streaming.active_conversations import (
    ConversationState, get_conversation_state, 
    end_and_save_conversation, add_to_conversation,
    new_thread_id, check_thread_exists, cancel_tool_tasks
)

router = APIRouter()

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
    logger = configure_logging(__name__)
    read_history=False
    if not thread_id:
        thread_id = await new_thread_id()
        logger.info(f"Starting a new conversation with thread_id: {thread_id}...")
    else:
        logger.info(f"Resuming conversation with thread_id: {thread_id}...")
        if not await check_thread_exists(thread_id):
            logger.info(f"Existing conversation is not found in the registry: {thread_id} ! "\
                        "It will be registered after the thread history is read.")
            read_history = True

    user_input = input or None
    if user_input is None:
        raise HTTPException(
            status_code=422, 
            detail="Input not found. Please provide a non-empty input in the query parameters or the headers, of type String."
            )

    model_name = chatbot or default_chatbot()

    user_name = Auth.username
    logger = configure_logging(__name__, thread_id=thread_id, user_id=user_name)

    if not Auth.vault_url:
        raise HTTPException(
            status_code=422,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )
    
    try:
        # Get thread storage
        Storage = await get_thread_storage(vault_url=Auth.vault_url, user_name=user_name, thread_id=thread_id)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    system_prompt = get_entire_prompt(user_name, thread_id, model_name)

    logger.info(
        "Streaming response",
        extra={
            "thread_id": thread_id,
            "user_id": user_name,
            "model_name": model_name,
        },
    )

    try:
        await prepare_for_stream(
            thread_id=thread_id, 
            user_id=user_name,
            Auth=Auth,
            Storage=Storage,
            read_history=read_history,
            logger=logger,
        )
    except Exception as e:
        msg = f"Stream preparation has failed: {e}"
        logger.exception(msg, extra={"thread_id": thread_id, "user_id": user_name})
        # Normalize response to a clean HTTP 500 instead of a partial stream
        raise HTTPException(status_code=500, detail="Internal Server Error: {e}")

    async def event_stream():

        last_check = time.monotonic()
        async for variant in run_stream(
            model=model_name,
            thread_id=thread_id,
            user_input=user_input,
            system_prompt=system_prompt,
            logger=logger,
        ):
            for data in _sse_data(from_sv_to_json(variant)):
                yield data

            now = time.monotonic()
            # Check if there is STOP request from
            if now-last_check > CHECK_INTERVAL:
                last_check = now
                state = await get_conversation_state(thread_id)
                if state == ConversationState.STOPPING:
                    end_v = SVStreamEnd(message="Stream is stopped by user.")
                    yield _sse_data(from_sv_to_json(end_v))
                    await add_to_conversation(thread_id, [end_v])
                    await cancel_tool_tasks(thread_id)
                    await end_and_save_conversation(thread_id, Storage)
                    logger.info("Stopped streaming after client request", extra={"thread_id": thread_id, "user_id": user_name})
                    return
                
        await end_and_save_conversation(thread_id, Storage)
        logger.info("Completed streaming and saved conversation", extra={"thread_id": thread_id, "user_id": user_name})

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform", 
            },
    )
