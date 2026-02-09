from __future__ import annotations

from typing import Union, Tuple

from fastapi import APIRouter, HTTPException, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.storage.helpers import Variant, PREFIX_MAP
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/searchthreads", dependencies=[AuthRequired])
async def search_threads(
    num_threads: int,
    page: int,
    query: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Returns the threads matching the query string.
    Requires x-freva-vault-url header for DB bootstrap.
    """
    logger = configure_logging(__name__, user_id=auth.username)

    if not auth.username:
        raise HTTPException(
            status_code=422,
            detail="Missing user_id (auth).",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")
    
    if not query:
        raise HTTPException(
            status_code=422,
            detail="Missing query parameter.",
        )
        
    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    # Decide search mode (topic vs variant)
    mode, _query = parse_query_mode(query)

    try:
        if mode == "variant":
            variant, content = _query
            total_num_threads, threads = await Storage.query_by_variant(auth.username, variant, content, num_threads)
        else:
            total_num_threads, threads = await Storage.query_by_topic(auth.username, _query, num_threads)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to query threads.")

    return [
        [
            {
                "user_id": t.user_id, 
                "thread_id": t.thread_id,
                "date": t.date,
                "topic": t.topic,
                "content": t.content,
            }
            for t in threads
        ], 
        total_num_threads
    ]


def parse_query_mode(query: str) -> Union[str, Tuple[Variant, str]]:
    """
    Returns:
      - query string OR
      - (variant, content) if prefix is recognized
    If prefix is unknown, sliently falls back to plain query search.
    """
    q = query.strip().lower() # case-insensitive search
    if ":" not in q:
        return "topic", q

    prefix, content = q.split(":", 1)
    prefix = prefix.strip()
    content = content.strip()

    variant = PREFIX_MAP.get(prefix)
    if variant:
        return "variant", (variant, content)

    # unknown prefix falls back to topic query, not error
    return "topic", q
