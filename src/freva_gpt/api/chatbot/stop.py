from fastapi import APIRouter, HTTPException, Query
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import AuthRequired
from freva_gpt.services.streaming.active_conversations import request_stop

router = APIRouter()


@router.get("/stop", dependencies=[AuthRequired])
async def stop_get(
    thread_id: str
    | None = Query(default=None, description="Thread to stop (optional)")
):
    """
    Signal that a conversation should stop streaming and cancel in-flight tools.
    Returns True if the conversation was found and updated.
    """
    logger = configure_logging(__name__, thread_id=thread_id)

    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Thread ID is missing. Please provide a thread_id in the query parameters.",
        )

    ok = await request_stop(thread_id)
    logger.debug("Initiated stop request", extra={"thread_id": thread_id})

    if ok:
        return {"ok": ok, "body": "Conversation stopped."}
    else:
        return {
            "ok": True,
            "body": "Conversation with given thread-id was never registered.",
        }
        # raise ValueError(f"Conversation with given thread ID not found: {thread_id}")
