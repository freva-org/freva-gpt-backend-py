from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

import httpx

from freva_gpt.core.settings import get_settings

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
# Single base URL for the LiteLLM proxy (OpenAI-compatible).
# Inside docker-compose: http://litellm:4000
# For local dev: http://localhost:4000


def _completions_url() -> str:
    s = get_settings()
    return f"{s.LITE_LLM_ADDRESS.rstrip('/')}/v1/chat/completions"


# Optional bearer to satisfy proxies that require it.
AUTH_TOKEN = os.getenv("FREVAGPT_OPENAI_API_KEY", "")


def _passthrough_params(params: Dict[str, Any] | None) -> Dict[str, Any]:
    # Tiny wrapper to allow future param sanitization
    return dict(params or {})


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    # Authorization header is not required for Ollama models,
    # but sending it (when available) doesn’t hurt and satisfies OpenAI-routed calls.
    if AUTH_TOKEN:
        h["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return h


async def _post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=_headers())
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data


def _extract_text(resp: Any) -> str:
    try:
        text: str = resp["choices"][0]["message"]["content"]
        return text
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API (non-streaming)
# ---------------------------------------------------------------------------
async def acomplete(
    *,
    model: str,
    messages: Iterable[Dict[str, Any]],
    stream: bool = False,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
    **request_params: Any,
) -> Dict[str, Any] | AsyncIterator[Dict[str, Any]]:
    """
    Call LiteLLM /v1/chat/completions.
    - stream=False: return JSON dict
    - stream=True: return **async iterator** yielding OpenAI-style stream chunks (dicts)
    """
    url = _completions_url()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "stream": stream,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if extra:
        payload.update(extra)
    payload.update(_passthrough_params(None))
    if request_params:
        payload.update(_passthrough_params(request_params))

    if not stream:
        return await _post_json(url, payload)

    timeout = httpx.Timeout(60.0, read=300.0, write=30.0, connect=30.0)
    client = httpx.AsyncClient(timeout=timeout)

    async def _aiter() -> AsyncIterator[Dict[str, Any]]:
        try:
            async with client.stream(
                "POST", url, json=payload, headers=_headers()
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
        finally:
            await client.aclose()

    return _aiter()


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


def first_message(resp: Dict[str, Any]) -> Any:
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
    "acomplete",
    "first_text",
    "tool_calls",
    "first_message",
]
