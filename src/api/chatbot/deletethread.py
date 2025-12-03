from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Depends
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage

router = APIRouter()


@router.get("/deletethread", dependencies=[AuthRequired])
async def delete_thread(
    thread_id: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Removes the thread from storage of the authenticated user.
    Requires x-freva-vault-url header for DB bootstrap.
    """
    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not auth.vault_url:
        raise HTTPException(status_code=503, detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    Storage = await get_thread_storage(vault_url=auth.vault_url)

    ok = await Storage.delete_thread(thread_id)

    if ok:
        return {"ok": ok, "body": "Successfully removed thread from history."}
    else:
        return {"ok": ok, "body": f"Failed to remove thread from storage: {thread_id}"}