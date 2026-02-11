from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Depends

from src.services.service_factory import Authenticator, AuthRequired, auth_dependency, get_thread_storage, ThreadStorage
from src.services.streaming.active_conversations import save_feedback_to_registry

router = APIRouter()


@router.get("/userfeedback", dependencies=[AuthRequired])
async def user_feedback(
    thread_id: str,
    feedback_at_index: int, 
    feedback: str,
    auth: Authenticator = Depends(auth_dependency),
):
    """
    Add or remove user feedback for a specific message within a thread.

    This endpoint allows an authenticated user to attach feedback to a
    specific entry (by index) in an existing conversation thread.
    If `feedback` is set to `"remove"`, the feedback at the given index
    will be deleted instead.
    Requires a valid authenticated user and vault-url.

    Parameters:
        thread_id (str):
            Unique identifier of the thread containing the content.
            Must correspond to an existing stored thread.

        feedback_at_index (int):
            Zero-based index of the message within the thread content
            where feedback should be added or removed.
            Must be within the bounds of the thread content list.

        feedback (str):
            The feedback value to store (e.g., "up", "down", text note).
            If set to the literal string `"remove"`, the existing feedback
            at the specified index will be deleted.

    Dependencies:
        auth (Authenticator):
            Injected authentication object containing:
            - username (used as user_id)
            - vault_url (used to resolve thread storage)

    Returns:
        dict:
            - {"Successfully saved user feedback."} on successful save.
            - {"Successfully removed user feedback."} on successful deletion.

    Raises:
        HTTPException (422):
            - Missing thread_id
            - Missing vault_url
            - feedback_at_index out of bounds

        HTTPException (404):
            - Thread not found
            - Feedback not found at specified index (on removal)

        HTTPException (500):
            - Error reading thread file
            - Failure while saving or deleting feedback

        HTTPException (503):
            - Failure connecting to thread storage (MongoDB)
    """

    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not auth.vault_url:
        raise HTTPException(
            status_code=422, 
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.")

    try:
        # Thread storage 
        Storage = await get_thread_storage(vault_url=auth.vault_url)
    except:
        raise HTTPException(status_code=503, detail="Failed to connect to MongoDB.")

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
            status_code=422,
            detail="feedback_at_index outside content range! Please review query parameters!",
        )

    if feedback != "remove":
        try:
            await save_feedback(Storage, thread_id, auth.username, content_json, feedback_at_index, feedback)
            return {"Successfully saved user feedback."}
        except:
            raise HTTPException(status_code=500, detail="Failed to save user feedback: {thread_id}")
    else:
        # TODO: delete feedback when user deletes thread?
        if "feedback" not in content_json[feedback_at_index].keys():
            raise HTTPException(status_code=404, detail=f"Feedback not found at index {feedback_at_index}: {thread_id}")
        try:
            await delete_feedback(Storage, thread_id, auth.username, content_json, feedback_at_index)
            return {"Successfully removed user feedback."}
        except:
            raise HTTPException(status_code=500, detail=f"Failed to delete user feedback: {thread_id}")


async def save_feedback(storage: ThreadStorage, thread_id, user_id, content, f_ind, feedback):
    try:
        await storage.save_feedback(thread_id, user_id, content, f_ind, feedback)
        await save_feedback_to_registry(thread_id, f_ind, feedback)
    except:
        raise


async def delete_feedback(storage: ThreadStorage, thread_id, user_id, content, f_ind):
    try:
        await storage.delete_feedback(thread_id, user_id, content, f_ind)
        await save_feedback_to_registry(thread_id, f_ind, "remove")
    except:
        raise
