# Rust → Python mapping:
#   - src/auth.rs  → authorize_or_fail_fn(..), get_username_from_token(..)
# Ported semantics:
#   • Require AUTH_KEY in env (validated again here; Rust uses OnceCell and would 500 if missing)
#   • Prefer Authorization: Bearer <token> (or x-freva-user-token) + x-freva-rest-url
#   • Check token by calling <rest_url>/api/freva-nextgen/auth/v2/systemuser (path normalization identical to Rust)
#   • ALLOW_FALLBACK_OLD_AUTH = False  (exactly as in Rust)
#   • Error codes/messages mirror Rust (422/400/401/502/503)
# TODO: implement "is_guest", "get_mongodb_uri"

from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status

from src.core.settings import get_settings

log = logging.getLogger(__name__)

# Whether or not the old auth system should be used as a fallback.
ALLOW_FALLBACK_OLD_AUTH = False

# Shared async client (Rust uses a Lazy reqwest::Client)
_httpx_client: Optional[httpx.AsyncClient] = None
def _client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=20.0)
    return _httpx_client

async def close_http_client() -> None:
    global _httpx_client
    if _httpx_client is not None:
        await _httpx_client.aclose()
        _httpx_client = None

def _bearer_token_from_header(header_val: str) -> str:
    # The header can be any value, we only allow String.
    if not isinstance(header_val, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Authorization header is not a valid UTF-8 string.",
        )
    # The Authentication header is a Bearer token, so we need to extract the token from it.
    if not header_val.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Authorization header is not a Bearer token. Please use the Bearer token format.",
        )
    return header_val[len("Bearer ") :]


def _normalize_systemuser_path(rest_url: str) -> str:
    """
    The entire url ending is "/api/freva-nextgen/auth/v2/systemuser",
    But it sometimes doesn't send the api and nextgen part, so we need to add it ourselves.
    """
    if rest_url.endswith("/api/freva-nextgen/auth/v2/systemuser"):
        return ""
    if rest_url.endswith("/api/freva-nextgen/"):
        return "auth/v2/systemuser"
    if rest_url.endswith("/api/freva-nextgen"):
        return "/auth/v2/systemuser"
    return "/api/freva-nextgen/auth/v2/systemuser"

async def authorize_or_fail(request: Request) -> Optional[str]:
    """
    Authorization function, used by all "api/chatbot/*" endpoints.
    Checks the Authorization header (Bearer token) or x-freva-user-token + x-f
    The user might send both an auth_key in the query string and an Authorization header.
    The header takes priority, but a warning is emitted if they don't match.
    Returns:
      - str username if resolved via token check
      - None if (old) query-key fallback were used (disabled here)
    Errors:
      - 500 if AUTH_KEY unset
      - else 422/400/401/502/503 same as Rust
    """
    settings = get_settings()
    # Rust would 500 if AUTH_KEY OnceCell not initialized
    if not settings.AUTH_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No auth key found in the environment; Authorization failed.",
        )

    q = request.query_params
    maybe_key = q.get("auth_key")
    headers = request.headers

    # Checking Authorization header OR x-freva-user-token
    header_val = headers.get("Authorization") or headers.get("x-freva-user-token")

    if header_val:
        # -> Bearer flow
        try:
            token = _bearer_token_from_header(header_val)
        except HTTPException as e:
            # Return 422 for non-Bearer
            raise e

        rest_url = headers.get("x-freva-rest-url")
        if not rest_url:
            # 400 when rest URL header missing
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authentication not successful; please use the nginx proxy. (rest)",
            )

        try:
            username = await get_username_from_token(token, rest_url)
            # make username available to downstream handlers
            request.state.username = username
            return username  
        except HTTPException as token_err:
            # if fallback disabled, return token_err
            if not ALLOW_FALLBACK_OLD_AUTH:
                raise token_err
            # (Fallback path disabled by default; if ever enabled:)
            if maybe_key is not None:
                if maybe_key != settings.AUTH_KEY:
                    raise token_err
                # Authenticated without username
                return None
            raise token_err

    # No Authorization header -> possible (currently disabled) query key fallback
    if maybe_key is not None:
        if not ALLOW_FALLBACK_OLD_AUTH:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No Authorization header found. Please use the Bearer token format.",
            )
        # If the key is not the same as the one in the environment, we'll return a 401.
        if maybe_key != settings.AUTH_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized request."
            )
        return None

    # Neither header nor query key → 401 when fallback disabled
    if ALLOW_FALLBACK_OLD_AUTH:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No key provided in the request. Please set the auth_key in the query parameters.",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Some necessary field weren't found in...in, check whether the nginx proxy and sets the right headers.",
    )

# Convenience alias for router-wide protection:
AuthRequired = Depends(authorize_or_fail)


async def get_username_from_token(token: str, rest_url: str) -> str:
    """Recives a token, checks it against the URL provided in the header and returns the username."""
    path = _normalize_systemuser_path(rest_url)
    url = f"{rest_url}{path}"
    log.debug("Token check URL: %s", url)

    try:
        resp = await _client().get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        # Rust: ServiceUnavailable on request error to vault/rest
        log.error("Error sending request to systemuser endpoint: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Error sending token check request, is the URL correct?",
        )

    # on any non-2xx from systemuser, return 401 immediately (don’t parse JSON)
    if not (200 <= resp.status_code < 300):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token check failed, the token is likely not valid (anymore).",
        )

    # 2xx: parse JSON and extract username/detail
    text = resp.text
    log.debug("Token check success status=%s body=%s", resp.status_code, text[:500])
    try:
        data = resp.json()
    except Exception as e:
        log.error("Error parsing token check response: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Token check response is malformed, not valid JSON.",
        )

    username = data.get("pw_name")
    if isinstance(username, str) and username:
        return username

    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        # Unauthorized with "Token check failed: {detail}"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token check failed: {detail}",
        )

    # 502 when JSON has no pw_name and no detail
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Token check response is malformed, no username found.",
    )