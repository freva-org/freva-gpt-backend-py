from fastapi import APIRouter, HTTPException
from typing import List
from yaml.error import YAMLError

from src.core.available_chatbots import available_chatbots
from src.services.service_factory import AuthRequired

router = APIRouter()

@router.get("/availablechatbots", response_model=List[str], dependencies=[AuthRequired])
async def available_chatbots_endpoint() -> List[str]:
    """
    Available Chatbots

    Statically returns the list of available chatbots as a List.
    Requires a valid authentication.

    The returned list of strings can be used by the frontend to select 
    a model elsewhere. If no model is specified, the first item of this 
    list is the default.
    """
    try:
        # Return ordered list of model names from litellm_config.yaml
        chatbot_list = available_chatbots()

        return [c for c in chatbot_list if "embed" not in c]
    
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=500,
            detail="LiteLLM config file not found.",
        )

    except YAMLError as e:
        raise HTTPException(
            status_code=500,
            detail="Failed to parse LiteLLM config file.",
        ) from e

    except ValueError as e:
        raise HTTPException(
            status_code=500, 
            detail="No available chatbots found in LiteLLM config file.",
        ) from e

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )
