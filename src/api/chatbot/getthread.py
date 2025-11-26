from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Query, Depends
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.streaming.stream_variants import StreamVariant, is_prompt


router = APIRouter()


def _post_process(v: list[StreamVariant]) -> list[StreamVariant]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in v if not is_prompt(item)]
    cleaned: list[StreamVariant] = []
    for i, v in enumerate(items):
        type_v = v.get("variant")
        if type_v == "StreamEnd":
            is_last = (i == len(items) - 1)
            if (not is_last) or ("unexpected manner" in (getattr(v, "message", "") or "").lower()):
                continue
        cleaned.append(v)
    return cleaned


@router.get("/getthread", dependencies=[AuthRequired])
async def get_thread(
    request: Request, 
    thread_id: str | None = Query(None),
    auth: Authenticator = Depends(auth_dependency),
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

    if not auth.vault_url:
        raise HTTPException(status_code=503, detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    # Thread storage 
    Storage = await get_thread_storage(vault_url=auth.vault_url)

    try:
        content = await Storage.read_thread(thread_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading thread file.")

    content = _post_process(content)
    return content