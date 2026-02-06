from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

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
    Create a new thread that branches off from `source_thread_id` at `fork_from_index`.
    Returns the new thread_id and the trimmed base history.
    """
    user_name = Auth.username
    vault_url = Auth.vault_url

    if not vault_url:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vault URL not found in headers",
        )

    # Thread storage 
    Storage = await get_thread_storage(vault_url=Auth.vault_url)

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
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="fork_from_index outside content range! Please review query parameters!",
        )

    # Cut history BEFORE the edited user message
    # (drop the original user message and everything after)
    base_json = orig_json[:fork_from_index]

    base_sv = [from_json_to_sv(v) for v in base_json]

    new_id = await new_thread_id()
    root_thread_id = source_thread_id  # TODO: if there are many changes we need to track down the original root

    await Storage.save_thread(
        thread_id=new_id,
        user_id=user_name,
        content=base_sv,
        root_thread_id=root_thread_id,
        parent_thread_id=source_thread_id,
        fork_from_index= fork_from_index,
    )

    # Return the new thread_id and the base history
    return {
        "new_thread_id": new_id,
        "history": base_json,
    }
