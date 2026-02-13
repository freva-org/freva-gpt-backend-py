from __future__ import annotations
from typing import List, Dict

from fastapi import APIRouter, HTTPException, Query, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import StreamVariant, is_prompt, SVStreamEnd, from_sv_to_json
from src.services.streaming.stream_orchestrator import prepare_for_stream
from src.services.streaming.active_conversations import get_conv_messages
from src.core.logging_setup import configure_logging


router = APIRouter()


def _post_process(v: List[StreamVariant]) -> List[StreamVariant]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in v if not is_prompt(item)]
    cleaned: List[Dict] = []
    for i, v in enumerate(items):
        if isinstance(v, SVStreamEnd):
            is_last = (i == len(items) - 1)
            if (not is_last) or ("unexpected manner" in (getattr(v, "message", "") or "").lower()):
                continue
        cleaned.append(from_sv_to_json(v))
    return cleaned


@router.get("/getthread", dependencies=[AuthRequired])
async def get_thread(
    thread_id: str | None = Query(None),
    Auth: Authenticator = Depends(auth_dependency),
):
    """
    Retrieve a Chat Thread.

    Returns the full conversation content of a specific thread as a list
    of JSON objects.
    Requires a valid authenticated user and vault-url.

    Parameters:
        thread_id (str | None):
            The unique identifier of the thread to retrieve. Must be provided
            as a query parameter.

    Dependencies:
        Auth (Authenticator): Injected authentication object containing 
            username and vault_url 

    Returns:
        List[dict]:
            A list of conversation message objects representing the thread
            history after post-processing.

    Raises:
        HTTPException (422):
            - If `thread_id` is missing or empty.
            - If the vault URL header is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (404):
            - If the requested thread does not exist.
        HTTPException (500):
            - If an error occurs while reading or processing the thread.
    """

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not Auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String."
        )

    logger = configure_logging(__name__, thread_id=thread_id, user_id=Auth.username)

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=Auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    try:
        await prepare_for_stream(
            thread_id=thread_id, 
            user_id=Auth.username,
            Auth=Auth,
            Storage=Storage,
            read_history=True,
            logger=logger,
        )
    except FileNotFoundError:
        logger.exception("Thread not found.", extra={"thread_id": thread_id})
        raise HTTPException(status_code=404, detail="Thread not found.")
    except ValueError as e:
        logger.exception(f"Error reading thread file: {e}", extra={"thread_id": thread_id})
        raise HTTPException(status_code=500, detail=f"Error reading thread file: {e}")
        
    content = await get_conv_messages(thread_id)

    content = _post_process(content)

    logger.info("Fetched thread content.", extra={"thread_id": thread_id, "user_id": Auth.username})

    return content
