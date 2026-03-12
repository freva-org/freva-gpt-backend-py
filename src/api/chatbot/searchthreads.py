from __future__ import annotations
from src.services.storage.mongodb_storage import ThreadStorage

from typing import Union, Tuple, Literal, Optional

from fastapi import APIRouter, HTTPException, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.services.storage.helpers import Variant, PREFIX_MAP
from src.core.logging_setup import configure_logging

router = APIRouter()


@router.get("/searchthreads", dependencies=[AuthRequired])
async def search_threads(
    num_threads: Optional[int],
    page: Optional[int],
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
            The maximum number of matching threads to return. Optional, defaults to 20.
        page (int):
            The page number for pagination (reserved for paging logic). Optional, starts at 0.
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
        Storage: ThreadStorage = await get_thread_storage(vault_url=auth.vault_url)
    except Exception as e:
        logger.warning("Failed to connect to MongoDB: %s", e)
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    # Decide search mode (topic vs variant)
    mode_and_query = parse_query_mode(query)

    num_threads = num_threads or 20  # default to 20 if not provided
    page = page or 0  # default to 0 if not provided

    try:
        if mode_and_query[0] == "variant":
            variant: Variant = mode_and_query[1][0]
            content: str = mode_and_query[1][1]
            total_num_threads, threads = await Storage.query_by_variant(auth.username, variant, content, num_threads, page)
        else:
            _query: str = mode_and_query[1]
            total_num_threads, threads = await Storage.query_by_topic(auth.username, _query, num_threads, page)
    except Exception as e:
        logger.warning("Failed to query threads: %s", e)
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


def parse_query_mode(query: str) -> Union[tuple[Literal["topic"], str], tuple[Literal["variant"], Tuple[Variant, str]]]:
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

    variant: Variant | None = PREFIX_MAP.get(prefix)
    if variant:
        return "variant", (variant, content)

    # unknown prefix falls back to topic query, not error
    return "topic", q
