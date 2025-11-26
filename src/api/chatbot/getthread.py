from __future__ import annotations
from typing import List, Dict

from fastapi import APIRouter, HTTPException, Request, Query, Depends
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import StreamVariant, is_prompt, SVStreamEnd, from_sv_to_json
from src.services.streaming.stream_orchestrator import prepare_for_stream
from src.services.streaming.active_conversations import get_conv_messages


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
    request: Request, 
    thread_id: str | None = Query(None),
    Auth: Authenticator = Depends(auth_dependency),
):
    """
    Returns the content of a thread as list of JSON.
    Rust parity:
    - Requires query param thread_id
    - Requires header x-freva-vault-url
    - Removes Prompt variants
    """
    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not Auth.vault_url:
        raise HTTPException(status_code=503, detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    # Thread storage 
    Storage = await get_thread_storage(vault_url=Auth.vault_url)

    try:
        prep_error = await prepare_for_stream(
            thread_id=thread_id, 
            user_id=Auth.username,
            Auth=Auth,
            Storage=Storage,
            read_history=True
        )
        if prep_error:
            return prep_error

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading thread file.")
        
    content = await get_conv_messages(thread_id)

    content = _post_process(content)

    return content