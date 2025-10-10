from __future__ import annotations

import json
import logging
import random
import string
from typing import Any, Dict, List, Optional, Tuple
import asyncio

from fastapi import APIRouter, HTTPException, Request, Query
from starlette.responses import StreamingResponse

from src.auth import AuthRequired
from src.core.available_chatbots import default_chatbot, model_supports_images
from src.core.prompting import get_entire_prompt, get_entire_prompt_json
from src.core.stream_variants import (
    SVAssistant, SVPrompt, SVServerError, SVServerHint, SVStreamEnd, SVUser,
    StreamVariant, help_convert_sv_ccrm,
)
from src.services.models.litellm_client import acomplete, first_text
from src.services.storage.thread_storage import append_thread, read_thread, recursively_create_dir_at_rw_dir

router = APIRouter()
log = logging.getLogger(__name__)

def _new_conversation_id(length: int = 32) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _to_wire_dict(v: StreamVariant) -> Dict[str, Any]:
    d = v.model_dump()
    kind = d["variant"]
    if kind == "User":
        return {"variant": kind, "content": d["text"]}
    if kind == "Assistant":
        return {"variant": kind, "content": d["text"]}
    if kind == "Prompt":
        return {"variant": kind, "content": d["payload"]}
    if kind == "ServerHint":
        return {"variant": kind, "content": d["data"]}
    if kind == "ServerError":
        return {"variant": kind, "content": d["message"]}
    if kind == "StreamEnd":
        return {"variant": kind, "content": d["message"]}
    if kind == "OpenAIError":
        return {"variant": kind, "content": d["message"]}
    if kind == "CodeError":
        payload = {"message": d["message"]}
        if d.get("call_id"):
            payload["id"] = d["call_id"]
        return {"variant": kind, "content": payload}
    return d

def _sse_data(data_obj: Dict[str, Any]) -> bytes:
    payload = json.dumps(data_obj, ensure_ascii=False)
    return f"{payload}\n\n".encode("utf-8")

@router.get("/streamresponse", dependencies=[AuthRequired])
async def streamresponse_get(
    request: Request,
    thread_id: Optional[str] = Query(None),
    user_input: Optional[str] = Query(None),   # preferred
    input: Optional[str] = Query(None),        # tolerated alias
    chatbot: Optional[str] = Query(None),
):
    """
    Rust-parity GET endpoint that streams tokens over SSE:
      ServerHint → User → Assistant (multiple incremental chunks) → StreamEnd
    """
    ui = user_input if user_input is not None else input
    model_name = chatbot or default_chatbot()
    username = getattr(request.state, "username", None)  # set by AuthRequired

    # Create new thread if none provided
    create_new = not thread_id or not thread_id.strip()
    if create_new:
        thread_id = _new_conversation_id()
    user_id = username or "anonymous"

    # Ensure per-thread storage dir
    try:
        recursively_create_dir_at_rw_dir(user_id, thread_id)
    except Exception:
        log.debug("ensure rw_dir failed", exc_info=True)

    # Build messages (either fresh prompt or from prior conversation)
    try:
        if create_new:
            base = get_entire_prompt(user_id, thread_id, model_name)
            prompt_json = get_entire_prompt_json(user_id, thread_id, model_name)
            append_thread(thread_id, [SVPrompt(payload=prompt_json)])
            messages = list(base)
        else:
            prior_conv = read_thread(thread_id)
            messages = help_convert_sv_ccrm(
                prior_conv,
                include_images=model_supports_images(model_name),
                include_meta=False,
            )
    except Exception as e:
        # Bind values outside the generator; don't reference 'e' inside the closure.
        msg = f"prompt/history assembly failed: {e}"
        err_v = SVServerError(message=msg)
        end_v = SVStreamEnd(message="Error")
        append_thread(thread_id, [err_v, end_v])

        async def _err():
            yield _sse_data(_to_wire_dict(err_v))
            yield _sse_data(_to_wire_dict(end_v))

        return StreamingResponse(
            _err(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Add current user input & persist hint+user
    messages.append({"role": "user", "content": ui or ""})
    hint = SVServerHint(data={"thread_id": thread_id})
    user_v = SVUser(text=ui or "")
    append_thread(thread_id, [hint, user_v])

    async def _gen():
        # Emit hint & user immediately so UI opens the assistant bubble
        yield _sse_data(_to_wire_dict(hint))

        accumulated_parts: list[str] = []
        try:
            # Request true streaming from LiteLLM
            resp = await acomplete(model=model_name, messages=messages, stream=True)  # async iterator or dict

            if hasattr(resp, "__aiter__"):
                # Stream chunks as they arrive
                async for chunk in resp:  # type: ignore
                    try:
                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            accumulated_parts.append(piece)
                            yield _sse_data({"variant": "Assistant", "content": piece})
                        if choice.get("finish_reason"):
                            break
                    except Exception:
                        continue
            else:
                # Fallback: provider ignored streaming → chunk the full text
                full_txt = first_text(resp) or ""
                if full_txt:
                    import re
                    for piece in re.findall(r"\S+\s*", full_txt):
                        accumulated_parts.append(piece)
                        yield _sse_data({"variant": "Assistant", "content": piece})

            # Persist final assistant + end marker
            final_text = "".join(accumulated_parts)
            assistant_v = SVAssistant(text=final_text)
            end_v = SVStreamEnd(message="Done")
            append_thread(thread_id, [assistant_v, end_v])

            yield _sse_data(_to_wire_dict(end_v))

        except asyncio.CancelledError:
            # Client disconnected; persist what we have
            final_text = "".join(accumulated_parts)
            assistant_v = SVAssistant(text=final_text)
            end_v = SVStreamEnd(message="Cancelled")
            append_thread(thread_id, [assistant_v, end_v])
            # connection is gone; don't yield more
        except Exception as e:
            err_v = SVServerError(message=str(e))
            end_v = SVStreamEnd(message="Error")
            append_thread(thread_id, [err_v, end_v])
            yield _sse_data(_to_wire_dict(err_v))
            yield _sse_data(_to_wire_dict(end_v))

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
