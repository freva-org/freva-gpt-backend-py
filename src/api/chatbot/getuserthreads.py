from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Query
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.core.auth import AuthRequired, ALLOW_FALLBACK_OLD_AUTH  # match Rust flags
from src.services.storage.mongodb_storage import get_database, read_threads

router = APIRouter()

def _get_user_id_from_request(request: Request) -> str | None:
    """
    Username is provided by AuthRequired (auth layer sets request.state.username)    
    """
    # 1) Preferred: identity from auth middleware
    user_id = getattr(getattr(request, "state", None), "username", None)

    # 2) Fallback: query param
    if not user_id and ALLOW_FALLBACK_OLD_AUTH:
        user_id = request.query_params.get("user_id")

    return user_id


@router.get("/getuserthreads", dependencies=[AuthRequired])
async def get_user_threads(request: Request):
    """
    Returns the latest 10 threads of the authenticated user.
    Requires x-freva-vault-url header for DB bootstrap.
    """
    user_id = _get_user_id_from_request(request)
    if not user_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing user_id (auth or query).",
        )

    vault_url = request.headers.get("x-freva-vault-url")
    if not vault_url:
        raise HTTPException(status_code=503, detail="No vault URL provided.")

    try:
        database = await get_database(vault_url)
    except Exception:
        raise HTTPException(status_code=503, detail="Failed to connect to the database.")

    threads = await read_threads(user_id, database)
    # FastAPI will jsonify dataclasses; if you need custom shape, map here.
    return [
        {
            "user_id": t.user_id,
            "thread_id": t.thread_id,
            "date": t.date,
            "topic": t.topic,
            # omit heavy content for listing? Rust returns full docs; keep parity:
            "content": t.content,
        }
        for t in threads
    ]
