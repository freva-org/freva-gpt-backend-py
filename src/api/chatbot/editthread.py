from fastapi import APIRouter, Depends, HTTPException

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import from_json_to_sv, from_sv_to_json, SVServerHint
from src.services.streaming.active_conversations import new_thread_id, check_thread_exists, initialize_conversation
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/editthread", dependencies=[AuthRequired])
async def edit_thread(
    source_thread_id: str,
    user_index: int,
    Auth: Authenticator = Depends(auth_dependency),
):
    """
    Fork an existing conversation thread at a given message index.

    This endpoint creates a new thread by copying the message history of an
    existing thread up to the edited message. The specified message and all 
    subsequent messages are discarded in the new branch, allowing the client 
    to replace or modify the conversation from that point onward.

    The newly created thread:
    - Receives a new unique `thread_id`
    - Stores the truncated message history
    - Keeps a reference to the original thread as its parent
    - Tracks the `user_index` for lineage metadata

    Parameters:
        source_thread_id (str):
            The ID of the existing thread to fork from.
        user_index (int):
            The zero-based index in reference to user variants in the history 
            where the fork should occur. The message at this index and everything 
            after it will be excluded from the new thread.

    Dependencies:
        Auth (Authenticator):
            Injected authentication object containing:
            - username (used as user_id)
            - vault_url (used to resolve thread storage)

    Returns:
        dict:
            {
                "new_thread_id": str,   # ID of the newly created thread
                "history": list         # Trimmed original history (JSON format)
            }

    Raises:
        HTTPException (422):
            - Missing `source_thread_id`
            - Missing `vault_url`
            - `user_index` out of bounds
        HTTPException (404):
            - Source thread not found
        HTTPException (500):
            - Error reading or saving thread
        HTTPException (503):
            - Storage backend connection failure

    Notes:
        - The original thread remains unchanged.
        - The new thread's `root_thread_id` is currently set to the
          `source_thread_id`. If deep branching is introduced, root tracking
          logic may require refinement.
    """
    user_name = Auth.username
    vault_url = Auth.vault_url

    if not source_thread_id:
        raise HTTPException(
            status_code=422,
            detail="Source thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not vault_url:
        raise HTTPException(
            status_code=422,
            detail="Vault URL not found in headers",
        )
    
    logger = configure_logging(__name__, thread_id=source_thread_id, user_id=user_name)

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=Auth.vault_url)
    except Exception:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    # Load original content
    try:
        orig_json = await Storage.read_thread(thread_id=source_thread_id)
        orig_sv = [from_json_to_sv(v) for v in orig_json]

        # In case the user edits an input during active stream, frontend calls /stop
        # right after /editthread. Not to throw 500 if the source thread is not registered,
        # we check and register it here.
        is_source_registered = await check_thread_exists(thread_id=source_thread_id)
        if not is_source_registered:
            # The frontend sends /stop for source thread right after /editthread.
            # If the source is not registered, this returns 404. To avoid this, we 
            # initialize it here. /stop will set conv state to STOPPED which will avoid 
            # streaming conflicts.
            await initialize_conversation(
                thread_id=source_thread_id,
                user_id=user_name,
                messages=orig_sv,
                auth=Auth,
                logger=logger
            )

    except FileNotFoundError:
        logger.exception("Cannot read source thread. Thread not found.")
        raise HTTPException(status_code=404, detail="Thread not found.")
    except Exception:
        logger.exception("Cannot read source thread. Error reading thread file.")
        raise HTTPException(status_code=500, detail="Error reading thread file.")
    
    # Count the number of user messages and check index within bounds
    user_message_count = sum(1 for msg in orig_json if msg.get("variant") == "User")
    if user_index < 0 or user_index >= user_message_count:
        raise HTTPException(
            status_code=422,
            detail="user_index outside user message range! Please review query parameters!",
        )
        
    # Find the position of the Nth user message
    user_msg_seen = 0
    fork_from_index = None
    for i, msg in enumerate(orig_json):
        if msg.get("variant") == "User":
            if user_msg_seen == user_index:
                fork_from_index = i
                break
            user_msg_seen += 1
    if fork_from_index is None:
        raise HTTPException(
            status_code=422,
            detail="Could not find the specified user message index! Please review query parameters!",
        )

    # Cut history BEFORE the edited user message
    # (drop the original user message and everything after)
    base_sv = orig_sv[:fork_from_index]

    new_id = await new_thread_id()
    logger.info(f"Continuing the edited thread with thread-id: {new_id}")
    logger = configure_logging(__name__, thread_id=new_id, user_id=Auth.username)
    base_sv = update_threadid_in_content(new_id, base_sv, logger=logger)

    root_thread_id = source_thread_id 
    
    try:
        await Storage.save_thread(
            thread_id=new_id,
            user_id=user_name,
            content=base_sv,
            root_thread_id=root_thread_id,
            parent_thread_id=source_thread_id,
            fork_from_index= fork_from_index,
        )
    except Exception:
        logger.exception(f"Failed to save new thread. Source thread id: {source_thread_id}.")
        raise HTTPException(status_code=500, 
                            detail="Failed to save new thread with edited user input.")

    base_json = [from_sv_to_json(sv) for sv in base_sv]

    # Return the new thread_id and the base history
    return {
        "new_thread_id": new_id,
        "history": base_json,
    }


def update_threadid_in_content(new_id: str, content: list, logger):
    if isinstance(content[0], SVServerHint):
        content[0] = SVServerHint(data={"thread_id": new_id})
        logger.info("Updated ServerHint with new thread-id.")
    else:
        if any(isinstance(c, SVServerHint) for c in content):
            logger.exception("ServerHint is in unexpected position in thread content!")
            raise ValueError("ServerHint is in unexpected position in thread content!")
        else:
            logger.info("ServerHint is missing in the thread content. It is inserted with the new thread-id.")
            content = [SVServerHint(data={"thread_id": new_id})] + content
    return content
