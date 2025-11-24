import logging

from fastapi import APIRouter, Query, HTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.core.auth import AuthRequired
from src.core.logging_setup import configure_logging
from src.services.streaming.active_conversations import request_stop

log = logging.getLogger(__name__)
configure_logging()

router = APIRouter()


@router.get("/stop", dependencies=[AuthRequired])
async def stop_get(
    thread_id: str | None = Query(default=None, description="Thread to stop (optional)")
):  
    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Thread ID is missing. Please provide a thread_id in the query parameters.",
        )
    
    log.debug(f"Trying to stop conversation with id: {thread_id}")

    ok = await request_stop(thread_id)

    if ok:
        return {"ok": ok, "body": "Conversation stopped."}
    else:
        raise ValueError(f"Conversation with given thread ID not found: {thread_id}")
