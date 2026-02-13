from fastapi import APIRouter, Depends, HTTPException

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import from_json_to_sv
from src.services.streaming.active_conversations import new_thread_id

router = APIRouter()

@router.get("/editthread", dependencies=[AuthRequired])
async def edit_thread(
    source_thread_id: str,
    fork_from_index: int,
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
    - Tracks the `fork_from_index` for lineage metadata

    Parameters:
        source_thread_id (str):
            The ID of the existing thread to fork from.

        fork_from_index (int):
            The zero-based index in the original thread history where the fork
            should occur. The message at this index and everything after it
            will be excluded from the new thread.

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
            - `fork_from_index` out of bounds
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

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=Auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    # Load original content
    try:
        orig_json = await Storage.read_thread(thread_id=source_thread_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading thread file.")

    # Check index within bounds
    if fork_from_index < 0 or fork_from_index >= len(orig_json):
        raise HTTPException(
            status_code=422,
            detail="fork_from_index outside content range! Please review query parameters!",
        )

    # Cut history BEFORE the edited user message
    # (drop the original user message and everything after)
    base_json = orig_json[:fork_from_index]

    base_sv = [from_json_to_sv(v) for v in base_json]

    new_id = await new_thread_id()
    root_thread_id = source_thread_id  # TODO: if there are many changes we need to track down the original root
    
    try:
        await Storage.save_thread(
            thread_id=new_id,
            user_id=user_name,
            content=base_sv,
            root_thread_id=root_thread_id,
            parent_thread_id=source_thread_id,
            fork_from_index= fork_from_index,
        )
    except:
        raise HTTPException(status_code=500, detail="Failed to save new thread with edited user input.")

    # Return the new thread_id and the base history
    return {
        "new_thread_id": new_id,
        "history": base_json,
    }
