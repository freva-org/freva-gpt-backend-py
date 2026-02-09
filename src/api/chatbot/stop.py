from fastapi import APIRouter, Query, HTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from src.services.service_factory import AuthRequired
from src.core.logging_setup import configure_logging
from src.services.streaming.active_conversations import request_stop, cancel_tool_tasks

router = APIRouter()


@router.get("/stop", dependencies=[AuthRequired])
async def stop_get(
    thread_id: str | None = Query(default=None, description="Thread to stop (optional)")
):  
    """
    Signal that a conversation should stop streaming and cancel in-flight tools.
    Returns True if the conversation was found and updated.
    """

    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Thread ID is missing. Please provide a thread_id in the query parameters.",
        )

    logger = configure_logging(__name__, thread_id=thread_id)

    ok = await request_stop(thread_id)
    logger.debug("Initiated stop request", extra={"thread_id": thread_id})

    if ok:
        return {"Conversation stopped."}
    else:
        raise HTTPException(status_code=500, detail="Conversation with given thread-id not found.")
