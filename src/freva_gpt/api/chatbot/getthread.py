from __future__ import annotations

from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT

from freva_gpt.core.logging_setup import configure_logging
from freva_gpt.services.service_factory import (
    Authenticator,
    AuthRequired,
    auth_dependency,
    get_thread_storage,
)
from freva_gpt.services.streaming.active_conversations import get_conv_messages
from freva_gpt.services.streaming.stream_orchestrator import prepare_for_stream
from freva_gpt.services.streaming.stream_variants import (
    StreamVariant,
    SVStreamEnd,
    from_sv_to_json,
    is_prompt,
)

router = APIRouter()


def _post_process(v: List[StreamVariant]) -> List[StreamVariant]:
    """Remove Prompt variants before returning, drop any StreamEnd except the final one, and drop 'unexpected manner' ones anywhere."""
    items = [item for item in v if not is_prompt(item)]
    cleaned: List[Dict] = []
    for i, v in enumerate(items):
        if isinstance(v, SVStreamEnd):
            is_last = i == len(items) - 1
            if (not is_last) or (
                "unexpected manner"
                in (getattr(v, "message", "") or "").lower()
            ):
                continue
        cleaned.append(from_sv_to_json(v))
    return cleaned


@router.get("/getthread", dependencies=[AuthRequired])
async def get_thread(
    thread_id: str | None = Query(None),
    Auth: Authenticator = Depends(auth_dependency),
):
    """
    Returns the content of a thread as list of JSON.
    Rust parity:
    - Requires query param thread_id
    - Requires header x-freva-vault-url
    - Removes Prompt variants
    """

    logger = configure_logging(
        __name__, thread_id=thread_id, user_id=Auth.username
    )

    if not thread_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Thread ID not found. Please provide thread_id in the query parameters.",
        )

    if not Auth.vault_url:
        raise HTTPException(
            status_code=503,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers, of type String.",
        )

    # Thread storage
    Storage = await get_thread_storage(vault_url=Auth.vault_url)

    try:
        await prepare_for_stream(
            thread_id=thread_id,
            user_id=Auth.username,
            Auth=Auth,
            Storage=Storage,
            read_history=True,
        )
    except FileNotFoundError:
        logger.exception("Thread not found.", extra={"thread_id": thread_id})
        raise HTTPException(status_code=404, detail="Thread not found.")
    except ValueError as e:
        logger.exception(
            f"Error reading thread file: {e}", extra={"thread_id": thread_id}
        )
        raise HTTPException(
            status_code=500, detail=f"Error reading thread file: {e}"
        )

    content = await get_conv_messages(thread_id)

    content = _post_process(content)

    logger.info(
        "Fetched thread content.",
        extra={"thread_id": thread_id, "user_id": Auth.username},
    )

    return content
