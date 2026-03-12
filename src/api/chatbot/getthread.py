from __future__ import annotations
from typing import List

from fastapi import APIRouter, HTTPException, Query, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import StreamVariant, is_prompt, SVStreamEnd, from_sv_to_json
from src.services.streaming.stream_orchestrator import get_conversation_history
from src.core.logging_setup import configure_logging


router = APIRouter()


def _post_process(variants: List[StreamVariant]) -> List[dict[str, str | list[str]]]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in variants if not is_prompt(item)]
    cleaned: List[dict[str, str | list[str]]] = []
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
    except Exception as e:
        logger.warning("Failed to connect to MongoDB", extra={"error": str(e)})
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    try:
        messages = await get_conversation_history(
            thread_id=thread_id, 
            Storage=Storage,
        )
        # Note: we have passed in a Storage and set read_history to True, so the prepare_for_stream will read the history and return it as StreamVariants.
        # So if the messages are None, it means there was no Storage to read from and we raise a 404.
        if not messages:
            raise FileNotFoundError(f"Thread with ID {thread_id} not found.")
    except FileNotFoundError:
        logger.exception("Thread not found.", extra={"thread_id": thread_id})
        raise HTTPException(status_code=404, detail="Thread not found.")
    except ValueError as e:
        logger.exception(f"Error reading thread file: {e}", extra={"thread_id": thread_id})
        raise HTTPException(status_code=500, detail=f"Error reading thread file: {e}")
        
    # Note: in the past, the content was retrieved from the Registry here, but not that the messages are returned directly from the prepare_for_stream, 
    # (Which, through the initialize_conversation, wrote to the Registry; that value was then read back here fallibly)
    # Since messages are Some, we can be sure that the content that would be returned by the retrieval from the Registry is the same as the messages. 
    content = _post_process(messages)

    logger.info("Fetched thread content.", extra={"thread_id": thread_id, "user_id": Auth.username})

    return content
