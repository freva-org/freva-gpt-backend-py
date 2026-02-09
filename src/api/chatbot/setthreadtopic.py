from __future__ import annotations

from fastapi import APIRouter, HTTPException,Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/setthreadtopic", dependencies=[AuthRequired])
async def set_thread_topic(
    thread_id: str,
    topic: str, 
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Updates the thread topic with user-given str of the authenticated user.
    Requires x-freva-vault-url header for DB bootstrap.
    """
    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    try:
        await Storage.update_thread_topic(thread_id, topic)
        logger.info("Updated thread topic", extra={"thread_id": thread_id, "user_id": auth.username})
        return {"Successfully updated thread topic."}
    except:
        logger.warning("Failed to update thread topic", extra={"thread_id": thread_id, "user_id": auth.username})
        raise HTTPException(status_code=500, detail=f"Failed to update thread topic.")
