from fastapi import APIRouter, HTTPException
from typing import Dict, Optional


router = APIRouter()


@router.get("/ping")
def ping():
    """ Simple liveness probe """
    return {"status": "ok"}


@router.get("/help")
def help(
    endpoint: Optional[str | None] = None
):
    """
    Retrieve help information for available API endpoints. 
    It targets users, providing help on how to use features. 

    If no endpoint is specified, this function returns a dictionary
    containing descriptions of all supported endpoints.

    If an endpoint name is provided, it returns the corresponding
    help text for that specific endpoint.

    Args:
        endpoint (Optional[str]): The name of the endpoint for which
            help information is requested.

    Returns:
        Dict[str, str] | str:
            - If `endpoint` is None, returns a dictionary mapping
              endpoint names to their descriptions.
            - If `endpoint` is provided and exists, returns the
              description string for that endpoint.

    Raises:
        HTTPException:
            422 error if the requested endpoint is not found.
    """

    help_dict: Dict[str, str] = {
        "deletethread": "Delete a conversation permanently. "\
            "Once deleted, the thread and its messages cannot be recovered.",
        "editthread": "Create a new conversation by branching off from a "\
            "point in an existing thread. This allows you to explore a "\
            "different direction without losing the original conversation.",
        "searchthreads": "Search your conversations by keywords to quickly "\
            "find specific topics. You can also use prefixes like 'user:', "\
            "'ai:', or 'code:' to search only within your messages, "\
            "FrevaGPT's replies, or code blocks. This helps you quickly "\
            "find specific topics, questions, or messages across your threads.",
        "setthreadtopic": "Rename a conversation. This helps you organize "\
            "and quickly identify your threads later.",
        "userfeedback": "Give a thumbs up or thumbs down to rate FrevaGPT's "\
            "response. You can update or remove your feedback later. Your "\
            "feedback helps us improve accuracy and usefulness over time.",
    }

    if not endpoint:
        return help_dict
    else:
        if endpoint in help_dict.keys():
            return help_dict.get(endpoint)
        else:
            raise HTTPException(
                status_code=422,
                detail="Help for the requested endpoint not found. " \
                "Please try another."
            )
