from fastapi import APIRouter, HTTPException
from typing import List
from yaml.error import YAMLError

from src.services.streaming.active_conversations import new_thread_id
from src.services.service_factory import AuthRequired

router = APIRouter()

@router.get("/newthread", response_model=str, dependencies=[AuthRequired])
async def generate_new_thread_id() -> str:
    """
    Request new thread-id.

    Requires a valid authenticated user.

    Returns:
        str: ID for new thread.

    Raises:
    
    """
    new_id = await new_thread_id()
    return new_id
