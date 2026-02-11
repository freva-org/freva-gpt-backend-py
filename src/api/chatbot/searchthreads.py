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
    query: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Search User Threads.

    Searches the authenticated user's conversation threads using a query
    string. Supports topic-based search and variant-based search, depending
    on the parsed query format.
    Requires a valid authenticated user and vault-url.

    Parameters:
        num_threads (int):
            The maximum number of matching threads to return.
        page (int):
            The page number for pagination (reserved for paging logic).
        query (str):
            The search query string. The query may be interpreted as:
                - A topic search (default mode), or
                - A variant-based search (if matching variant syntax).

    Dependencies:
        auth (Authenticator): Injected authentication object containing 
            username and vault_url 

    Returns:
        List[Any]:
            A two-element list containing:
                1. A list of matching thread metadata dictionaries, each including:
                   - user_id (str)
                   - thread_id (str)
                   - date (datetime | str)
                   - topic (str)
                   - content (Any)
                2. The total number of matching threads (int).

    Raises:
        HTTPException (422):
            - If the authenticated user ID is missing.
            - If the vault URL header is missing or empty.
            - If the query parameter is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If querying threads fails due to an internal error.
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
