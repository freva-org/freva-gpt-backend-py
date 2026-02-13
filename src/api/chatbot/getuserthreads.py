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
    Retrieve Recent User Threads.

    Returns the most recent conversation threads of the authenticated user,
    limited by the requested number.
    Requires a valid authenticated user and vault-url.

    Parameters:
        num_threads (int):
            The maximum number of recent threads to return.

    Dependencies:
        auth (Authenticator): Injected authentication object containing 
            username and vault_url 

    Returns:
        List[Any]:
            A two-element list containing:
                1. A list of thread metadata dictionaries, each including:
                   - user_id (str)
                   - thread_id (str)
                   - date (datetime | str)
                   - topic (str)
                   - content (Any)
                2. The total number of threads available for the user
                   (int), independent of the requested limit.

    Raises:
        HTTPException (422):
            - If the authenticated user ID is missing.
            - If the vault URL header is missing or empty.
        HTTPException (503):
            - If the storage backend (e.g., MongoDB) connection fails.
        HTTPException (500):
            - If fetching the user's thread history fails.
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
