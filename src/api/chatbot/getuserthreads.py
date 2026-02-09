from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage
from src.core.logging_setup import configure_logging

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
            status_code=422,
            detail="Missing user_id (auth).",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String."
        )

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

    try:
        threads, total_num_threads = await Storage.list_recent_threads(auth.username, limit=num_threads)

        logger.info(
            "Fetched recent threads",
            extra={"user_id": auth.username, "thread_count": len(threads), "requested": num_threads},
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
            total_num_threads
        ]
    except:
        raise HTTPException(status_code=500,
                            detail="Failed to fetch user history from storage.")
