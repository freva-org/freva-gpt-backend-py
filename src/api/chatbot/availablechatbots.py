from fastapi import APIRouter, Request
from typing import List

from src.core.available_chatbots import available_chatbots
from src.services.service_factory import AuthRequired

router = APIRouter()

@router.get("/availablechatbots", response_model=List[str], dependencies=[AuthRequired])
async def available_chatbots_endpoint(request: Request) -> List[str]:
    """
    Available Chatbots

    Statically returns the list of available chatbots as a List.
    Requires a valid auth (same semantics as Rust's `authorize_or_fail!`).

    The returned strings can be used by the frontend to select a model elsewhere.
    If no model is specified there, the first item of this list is the default.
    """
    # Return ordered list of model names from litellm_config.yaml
    return available_chatbots()
