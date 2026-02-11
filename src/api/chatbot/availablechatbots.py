from fastapi import APIRouter, HTTPException
from typing import List
from yaml.error import YAMLError

from src.core.available_chatbots import available_chatbots
from src.services.service_factory import AuthRequired

router = APIRouter()

@router.get("/availablechatbots", response_model=List[str], dependencies=[AuthRequired])
async def available_chatbots_endpoint() -> List[str]:
    """
    Retrieve Available Chatbots.

    Returns an ordered list of available chatbot model names defined in
    the LiteLLM config file. Models containing the substring "embed" are 
    excluded from the result.
    Requires a valid authenticated user.

    Returns:
        List[str]: A list of chatbot model identifiers. The list order
        reflects the order defined in the LiteLLM configuration. The
        first item in the list is considered the default model by the
        frontend if no explicit model is selected.

    Raises:
        HTTPException (500):
            - If the LiteLLM configuration file is not found.
            - If the configuration file cannot be parsed.
            - If no chatbot models are defined in the configuration.
            - For any other unexpected server error.
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
