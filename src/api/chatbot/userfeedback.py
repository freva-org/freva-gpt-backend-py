from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Depends
from starlette.status import HTTP_422_UNPROCESSABLE_ENTITY

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage

router = APIRouter()


@router.get("/userfeedback", dependencies=[AuthRequired])
async def user_feedback(
    thread_id: str,
    feedback_at_index: int, 
    feedback: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Updates the thread topic with user-given str of the authenticated user.
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

    # Load the thread content
    try:
        content_json = await Storage.read_thread(thread_id=thread_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Thread not found")
    except Exception:
        raise HTTPException(status_code=500, detail="Error reading thread file.")
    
    # Check if index within bounds
    if feedback_at_index < 0 or feedback_at_index >= len(content_json):
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="feedback_at_index outside content range! Please review query parameters!",
        )

    if feedback != "remove":
        ok = await Storage.save_feedback(thread_id, auth.username, content_json, feedback_at_index, feedback)
        if ok:
            return {"ok": ok, "body": "Successfully saved user feedback."}
        else:
            return {"ok": ok, "body": f"Failed to save user feedback: {thread_id}"}
    else:
        # TODO: delete feedback when user deletes thread?

        if "feedback" not in content_json[feedback_at_index].keys():
            return {"ok": False, "body": f"Feedback not found at index {feedback_at_index}: {thread_id}"}
        else:
            ok = await Storage.delete_feedback(thread_id, auth.username, content_json, feedback_at_index)
            if ok:
                return {"ok": ok, "body": "Successfully removed user feedback."}
            else:
                return {"ok": False, "body": f"Failed to delete user feedback: {thread_id}"}
