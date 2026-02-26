from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Depends
from starlette.responses import StreamingResponse

from src.core.logging_setup import configure_logging
from src.core.available_chatbots import default_chatbot, available_chatbots
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
    Stream Chatbot Response.

    Streams a chatbot response for a given user input using Server-Sent
    Events (NDJSON format). Acts as a HTTP wrapper delegating the actual 
    orchestration and model execution to the streaming backend.
    Requires a valid authenticated user and vault-url.

    Behavior:
        - Creates a new thread if `thread_id` is not provided.
        - Resumes an existing thread if `thread_id` is provided.
        - Reads thread history if the thread exists in storage but is not
          registered in the in-memory registry.
        - Selects the specified chatbot model or falls back to the default.
        - Persists the conversation after completion.
        - Periodically checks for stop requests and cancels streaming
          and in-flight tool executions if requested.

    Parameters:
        thread_id (Optional[str]):
            The unique identifier of the conversation thread. If not provided,
            a new thread ID is generated.
        input (Optional[str]):
            The user input message to send to the chatbot. Must be provided.
        chatbot (Optional[str]):
            The model name to use for the response. If not provided,
            the default chatbot model is selected.
    
    Dependencies:
        Auth (Authenticator): Injected authentication object containing 
            username and vault_url 

    Returns:
        StreamingResponse:
            A streaming HTTP response with media type
            ``application/x-ndjson`` containing incremental chatbot output.
            Response buffering is disabled.

    Raises:
        HTTPException (409):
            - If the provided `thread_id` is already active and streaming.
        HTTPException (422):
            - If the user input is missing or empty.
            - If the vault URL header is missing or empty.
            - If the specified chatbot model is not found in the available chatbots.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If stream preparation fails or an internal server error occurs
              before streaming begins.
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
        if await get_conversation_state(thread_id) == ConversationState.STREAMING:
            logger.warning(f"Conversation with thread_id: {thread_id} is already active and streaming. "
                        "Aborting the new streaming request to avoid conflicts.")
            raise HTTPException(
                status_code=409,
                detail=f"Conversation with thread_id: {thread_id} is already active and streaming. Please use a different thread_id or wait for the current stream to finish."
                )

    user_input = input or None
    if user_input is None:
        raise HTTPException(
            status_code=422, 
            detail="Input not found. Please provide a non-empty input in the query parameters or the headers, of type String."
            )

    model_name = chatbot or default_chatbot()
    available = available_chatbots()
    if model_name not in available:
        raise HTTPException(
            status_code=422,
            detail=f"Chatbot model '{model_name}' not found. Please provide a valid model name from the available chatbots: {available}."
        )


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
    except Exception as e:
        logger.exception("Failed to connect to MongoDB", extra={"thread_id": thread_id, "user_id": user_name, "error": str(e)})
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
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {e}")

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
