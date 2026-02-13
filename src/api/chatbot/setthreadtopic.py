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
    Update Thread Topic.

    Updates the topic/title of a specific conversation thread belonging
    to the authenticated user.
    Requires a valid authenticated user and vault-url.

    Parameters:
        thread_id (str):
            The unique identifier of the thread to update. Must be provided
            as a query parameter.
        topic (str):
            The new topic/title string to assign to the thread.
    
    Dependencies:
        auth (Authenticator): Injected authentication object containing 
            username and vault_url 

    Returns:
        dict:
            A success confirmation message if the thread topic was updated.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
            - If the vault URL header is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If updating the thread topic fails due to an internal error.
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
