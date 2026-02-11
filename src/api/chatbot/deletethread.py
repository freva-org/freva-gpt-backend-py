from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/deletethread", dependencies=[AuthRequired])
async def delete_thread(
    thread_id: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Delete a Chat Thread.

    Deletes a conversation thread from storage.
    Requires a valid authenticated user and vault-url.

    Parameters:
        thread_id (str):
            The unique identifier of the thread to delete. Must be provided
            as a query parameter.

    Dependencies:
        auth (Authenticator): Injected authentication object containing 
            username and vault_url

    Returns:
        dict:
            A confirmation message if the thread was deleted.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
            - If the vault URL header is missing or empty.
        HTTPException (500):
            - If deletion fails due to an internal storage error.
    """
    logger = configure_logging(__name__, thread_id=thread_id, user_id=auth.username)

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    Storage = await get_thread_storage(vault_url=auth.vault_url)

    try:
        await Storage.delete_thread(thread_id)
        logger.info("Deleted thread from storage", 
                    extra={"thread_id": thread_id, "user_id": auth.username})
        return {"Successfully removed thread from storage."}
    except:
        logger.warning("Failed to delete thread from storage", 
                       extra={"thread_id": thread_id, "user_id": auth.username})
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to remove thread from storage.")
