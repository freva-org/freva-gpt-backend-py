from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Query, Depends
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY
from starlette.responses import JSONResponse

from src.core.auth import AuthRequired
from src.services.storage.router import read_thread
from src.services.storage.mongodb_storage import get_database
from src.services.streaming.stream_variants import StreamVariant, is_prompt, SVStreamEnd

router = APIRouter()


def _post_process(v: list[StreamVariant]) -> list[StreamVariant]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in v if not is_prompt(item)]
    cleaned: list[StreamVariant] = []
    for i, v in enumerate(items):
        if isinstance(v, SVStreamEnd):
            is_last = (i == len(items) - 1)
            if (not is_last) or ("unexpected manner" in (getattr(v, "message", "") or "").lower()):
                continue
        cleaned.append(v)
    return cleaned

@router.get("/getthread", dependencies=[AuthRequired])
async def get_thread(request: Request, thread_id: str | None = Query(None)):
    """
    Returns the content of a thread as JSON (list of StreamVariants).
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

    vault_url = request.headers.get("x-freva-vault-url")
    if not vault_url:
        raise HTTPException(status_code=503, detail="No vault URL provided.")

    # Storage backend is MongoDB by default (matches Rust); Disk path doesn’t need DB
    database = await get_database(vault_url)

    try:
        content = await read_thread(thread_id, database=database)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading thread file.")

    return _post_process(content)
