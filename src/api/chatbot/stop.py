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
    Stop Active Conversation Streaming.

    Signals that an active conversation associated with the given thread
    should stop streaming and cancels any in-flight tool executions.
    Requires a valid authenticated user.

    Parameters:
        thread_id (str | None):
            The unique identifier of the thread whose streaming process
            should be stopped. Must be provided as a query parameter.

    Returns:
        dict:
            A confirmation message if the stop signal was successfully
            issued for the specified thread.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
        HTTPException (500):
            - If no active conversation with the given thread ID was found
              or the stop request failed.
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
