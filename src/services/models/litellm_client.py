from __future__ import annotations
import os
import json
import time
import asyncio
from typing import Any, Dict, List, Tuple, Optional

import requests  # sync HTTP; we'll run it in a thread

from src.settings import get_settings
# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
# Single base URL for the LiteLLM proxy (OpenAI-compatible).
# Inside docker-compose: http://litellm:4000
# On host (if you expose the port): http://localhost:4000

def _completions_url() -> str:
    s = get_settings()
    return f"{s.LITE_LLM_ADDRESS.rstrip('/')}/v1/chat/completions"

COMPLETIONS_URL = _completions_url()

# Optional bearer to satisfy proxies that require it.
AUTH_TOKEN = os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY") or ""

def _passthrough_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    # Tiny wrapper to allow future param sanitization
    return dict(params or {})

def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    # Many LiteLLM setups don’t require an Authorization header for Ollama models,
    # but sending it (when available) doesn’t hurt and satisfies OpenAI-routed calls.
    if AUTH_TOKEN:
        h["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return h

def _do_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.post(COMPLETIONS_URL, headers=_headers(), data=json.dumps(payload), timeout=300)
    # Let the caller see the real LiteLLM error payload
    resp.raise_for_status()
    return resp.json()

def _extract_text(resp: Any) -> str:
    try:
        return resp["choices"][0]["message"]["content"]
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# Public API (non-streaming)
# ---------------------------------------------------------------------------
async def acomplete(*, model: str, messages: List[Dict[str, Any]], **params) -> Dict[str, Any]:
    """
    Send a non-streaming /v1/chat/completions request to the LiteLLM proxy.
    - `model` must be the *config name* from your litellm_config.yaml (e.g., "qwen2.5:3b")
    - `messages` are standard OpenAI chat messages
    - extra **params go into the JSON body (after sanitization)
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "n": 1,
    }
    payload.update(_passthrough_params(params))

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _do_request(payload))

def first_text(resp: Any) -> str:
    return _extract_text(resp)


def tool_calls(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize tool/function-calls from a chat completion response.
    Works for OpenAI-style responses and returns [] if absent.
    """
    try:
        choices = resp.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        tc = msg.get("tool_calls") or []
        # Ensure it's a list of dicts
        if isinstance(tc, list):
            return [t for t in tc if isinstance(t, dict)]
        return []
    except Exception:
        return []

def first_message(resp: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Convenience: return the first assistant message dict or None.
    """
    try:
        choices = resp.get("choices") or []
        if not choices:
            return None
        return choices[0].get("message")
    except Exception:
        return None

__all__ = [
    "acomplete", "first_text",
    "tool_calls", "first_message",
]