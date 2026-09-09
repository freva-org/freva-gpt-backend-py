from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from climateclaw.core.available_chatbots import (
    available_chatbots,
    default_chatbot,
    default_chatbot_local,
)
from climateclaw.core.logging_setup import configure_logging
from climateclaw.core.prompting import get_entire_prompt
from climateclaw.services.service_factory import (
    Authenticator,
    AuthRequired,
    ThreadStorage,
    auth_dependency,
    get_thread_storage,
)
from climateclaw.services.storage.helpers import create_dir_at_cache
from climateclaw.services.streaming.active_conversations import (
    ConversationState,
    add_to_conversation,
    cancel_tool_tasks,
    check_thread_exists,
    end_and_save_conversation,
    get_conversation_state,
    new_thread_id,
)
from climateclaw.services.streaming.helpers import chunks
from climateclaw.services.streaming.stream_orchestrator import (
    prepare_for_stream,
    run_stream,
)
from climateclaw.services.streaming.stream_variants import (
    IMAGE,
    SVDict,
    SVServerHint,
    SVStreamEnd,
    from_sv_to_json,
)

router = APIRouter()

CHECK_INTERVAL = 3  # seconds, the interval to wait before check STOP request


class StreamResponseRequest(BaseModel):
    thread_id: str | None = None
    input: str | None = None
    chatbot: str | None = None
    store_thread: bool = True


def _sse_data(obj: SVDict) -> Generator[bytes]:
    if obj.get("variant") == IMAGE:
        image_b64 = obj.get("content")
        id = obj.get("id")
        CHUNK_SIZE = 8_192  # 8 KiB per JSON line

        # The fact that image_b64 will always be a string is implied by requiring the input to be a SVDict, which is only constructed from StreamVariants, which have strict types.
        for frag in chunks(image_b64, CHUNK_SIZE):  # type: ignore[arg-type]
            payload = json.dumps({"variant": "Image", "content": frag, "id": id})
            yield f"{payload}\n".encode()
    else:
        payload = json.dumps(obj)
        yield f"{payload}\n".encode()


@router.post("/streamresponse", dependencies=[AuthRequired])
async def streamresponse(
    request: StreamResponseRequest,
    http_request: Request,
    auth: Authenticator = Depends(auth_dependency),
    storage: ThreadStorage = Depends(get_thread_storage),
):
    """
    Stream Chatbot Response.

    Streams a chatbot response for a given user input using Server-Sent
    Events (NDJSON format). Acts as a HTTP wrapper delegating the actual
    orchestration and model execution to the streaming backend.
    Requires a valid authenticated user.

    Behavior:
        - Checks if thread-id exists in storage, resumes an existing thread
          if it already exists.
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
            username

    Returns:
        StreamingResponse:
            A streaming HTTP response with media type
            ``application/x-ndjson`` containing incremental chatbot output.
            Response buffering is disabled.

    Raises:
        HTTPException (409):
            - If the provided `thread_id` is already active and streaming.
        HTTPException (422):
            - If the thread id is missing or empty.
            - If the user input is missing or empty.
            - If the specified chatbot model is not found in the available chatbots.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If stream preparation fails or an internal server error occurs
              before streaming begins.
    """

    thread_id = request.thread_id
    input = request.input
    chatbot = request.chatbot
    store_thread = request.store_thread

    logger = configure_logging(__name__)

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread-id not found. Please request a new thread-id and provide it in the query parameters, of type String.",
        )

    if not input:
        raise HTTPException(
            status_code=422,
            detail="Input not found. Please provide a non-empty input in the query parameters, of type String.",
        )

    if chatbot == "local":
        model_name = default_chatbot_local()
    else:
        model_name = chatbot or default_chatbot()

    available = available_chatbots()
    if model_name not in available:
        raise HTTPException(
            status_code=422,
            detail=f"Chatbot model '{model_name}' not found. Please provide a valid model name from the available chatbots: {available}.",
        )

    user_name = auth.username
    logger = configure_logging(__name__, thread_id=thread_id, user_id=user_name)
    logger.info(
        f"User request: thread-id '{thread_id}', user-input '{input}', chatbot '{chatbot}'"
    )

    create_dir_at_cache(user_name, thread_id)

    is_new_thread = False

    # If the thread does not belong to this user, fork it and continue with a different thread_id
    thread_owner = await storage.get_user_id_for_thread(thread_id)
    if thread_owner and thread_owner != user_name:
        is_new_thread = True
        old_thread_id = thread_id
        thread_id = await new_thread_id()
        logger.info(
            f"Thread {old_thread_id} belongs to a different user ({thread_owner}). Forking the thread for the current user with new thread_id: {thread_id}..."
        )
        await storage.fork_thread(old_thread_id, thread_id, user_name)
        logger = configure_logging(__name__, thread_id=thread_id, user_id=user_name)

    # Check if thread-id exists in DB
    read_history = False
    if await storage.thread_exists(thread_id=thread_id):
        logger.info(f"Resuming conversation with thread_id: {thread_id}...")
        if not await check_thread_exists(thread_id):
            logger.info(
                f"Existing conversation is not found in the registry: {thread_id} ! "
                "It will be registered after the thread history is read."
            )
            read_history = True
        if await get_conversation_state(thread_id) == ConversationState.STREAMING:
            logger.warning(
                f"Conversation with thread_id: {thread_id} is already active and streaming. "
                "Aborting the new streaming request to avoid conflicts."
            )
            raise HTTPException(
                status_code=409,
                detail=f"Conversation with thread_id: {thread_id} is already active and streaming. Please use a different thread_id or wait for the current stream to finish.",
            )
    else:
        is_new_thread = True
        logger.info(f"Starting a new conversation with thread_id: {thread_id}...")

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
            Auth=auth,
            Storage=storage,
            read_history=read_history,
            logger=logger,
        )
    except ValueError as e:
        logger.warning(
            f"ValueError during stream preparation; most likely a race condition: {e}",
            extra={"thread_id": thread_id, "user_id": user_name},
        )
        raise HTTPException(
            status_code=409,
            detail="Another stream with the same thread_id is already active. Please wait for the current stream to finish or use a different thread_id.",
        )
    except Exception as e:
        msg = f"Stream preparation has failed: {e}"
        logger.exception(msg, extra={"thread_id": thread_id, "user_id": user_name})
        # Normalize response to a clean HTTP 500 instead of a partial stream
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {e}")

    async def event_stream():
        try:
            if is_new_thread:
                # Append ServerHint with thread_id
                hint_v = SVServerHint(content={"thread_id": thread_id})
                for data in _sse_data(from_sv_to_json(hint_v)):
                    yield data
                await add_to_conversation(
                    thread_id, [hint_v], storage=storage, store_thread=store_thread
                )

            stop_requested = False

            last_check = time.monotonic()
            async for variant in run_stream(
                model=model_name,
                thread_id=thread_id,
                user_input=input,
                system_prompt=system_prompt,
                storage=storage,
                store_thread=store_thread,
                logger=logger,
            ):
                for data in _sse_data(from_sv_to_json(variant)):
                    yield data

                now = time.monotonic()
                # Check if there is STOP request from
                if now - last_check > CHECK_INTERVAL:
                    last_check = now
                    state = await get_conversation_state(thread_id)
                    if state == ConversationState.STOPPING:
                        stop_requested = True

                        await cancel_tool_tasks(thread_id)

            if stop_requested:
                end_v = SVStreamEnd(content="Stream is stopped by user.")
                for data in _sse_data(from_sv_to_json(end_v)):
                    yield data
                await end_and_save_conversation(
                    thread_id, storage, store_thread=store_thread
                )
                logger.info(
                    "Stopped streaming after client request",
                    extra={"thread_id": thread_id, "user_id": user_name},
                )
                return

            await end_and_save_conversation(
                thread_id, storage, store_thread=store_thread
            )
            msg = (
                "Completed streaming and saved conversation"
                if store_thread
                else "Completed streaming without saving the conversation"
            )
            logger.info(
                msg,
                extra={"thread_id": thread_id, "user_id": user_name},
            )

        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            state = await get_conversation_state(thread_id)

            try:
                disconnected = await http_request.is_disconnected()
            except Exception:
                disconnected = None

            logger.error(
                "EVENT STREAM CANCELLED: "
                "thread=%s conv_state=%s http_disconnected=%s "
                "task=%r cancelling=%s error=%r args=%r",
                thread_id,
                state,
                disconnected,
                task,
                task.cancelling() if task else None,
                e,
                e.args,
                exc_info=True,
            )

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
        },
    )
