from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)
from freva_gpt.services.storage.helpers import PREFIX_MAP, Variant

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
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Missing user_id (auth).",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=503,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )

    if not query:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Missing query parameter.",
        )

    logger.info(
        f"Searching threads for user {auth.username} with query string: {query}"
    )

    Storage = await get_thread_storage(vault_url=auth.vault_url)

    q = query.strip().lower()  # case-insensitive search

    # Decide search mode (topic vs variant)
    mode = check_query_mode(q)

    try:
        if mode == "variant":
            try:
                variant, content = parse_variant_content(q)
            except ValueError:
                raise HTTPException(
                    status_code=HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Unknown prefix! Try one of the following: user, assistant, code, output",
                )
            total_num_threads, threads = await Storage.query_by_variant(
                auth.username, variant, content, num_threads
            )
        else:
            total_num_threads, threads = await Storage.query_by_topic(
                auth.username, q, num_threads
            )
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
        total_num_threads,
    ]


def check_query_mode(query: str) -> str:
    """
    Returns:
      - query_mode
    If prefix is unknown, sliently falls back to plain query search.
    """
    if ":" not in query:
        return "topic"
    else:
        return "variant"


def parse_variant_content(query: str) -> Tuple[Variant, str]:
    prefix, content = query.split(":", 1)
    prefix = prefix.strip()
    content = content.strip()

    variant = PREFIX_MAP.get(prefix)
    if variant:
        return variant, content
    else:
        raise ValueError
