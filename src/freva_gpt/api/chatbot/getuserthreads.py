from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)

router = APIRouter()


@router.get("/getuserthreads", dependencies=[AuthRequired])
async def get_user_threads(
    num_threads: int,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Returns the latest 10 threads of the authenticated user.
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

    Storage = await get_thread_storage(vault_url=auth.vault_url)

    threads, total_num_threads = await Storage.list_recent_threads(
        auth.username, limit=num_threads
    )

    logger.info(
        "Fetched recent threads",
        extra={
            "user_id": auth.username,
            "thread_count": len(threads),
            "requested": num_threads,
        },
    )

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
