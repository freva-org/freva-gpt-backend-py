from fastapi import HTTPException, status
import warnings
import httpx

from src.core.logging_setup import configure_logging
from .authenticator import Authenticator

log = configure_logging(__name__)


class FullAuthenticator(Authenticator):
    """
    Checks the Authorization header (Bearer token) or x-freva-user-token + x-f
    The user must send an Authorization header. No fallback logic to the 
    previous auth system.
    Returns:
      - self (Authenticator instance)
    Errors:
      - 422/400/401/502/503
    """
    async def run(self) -> "FullAuthenticator":
        settings = self.settings
        request = self.request

        q = request.query_params
        headers = request.headers

        # Checking Authorization header OR x-freva-user-token
        header_val = headers.get("Authorization") or headers.get("x-freva-user-token")

        # Checking vault_url. If it is not found, the exception is raised in the endpoints, where this is a must-have
        vault_url = headers.get("x-freva-vault-url")
        self.vault_url = vault_url

        if header_val:
            # -> Bearer flow
            try:
                token = bearer_token_from_header(header_val)
                self.access_token = token
            except HTTPException as e:
                # Raise exception for non-Bearer
                raise e

            # Checking rest_url
            rest_url = headers.get("x-freva-rest-url")
            if not rest_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Authentication not successful! RestURL not found. Please use the nginx proxy. (rest)",
                )
            self.rest_url = rest_url

            try:
                username = await get_username_from_token(token, rest_url, logger=configure_logging(__name__, user_id=self.username))
                self.username = username
                return self
            except HTTPException as err:
                raise err

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Some necessary field weren't found, check whether the nginx proxy and sets the right headers.",
        )
    
# ──────────────────── Helper functions ──────────────────────────────

def bearer_token_from_header(header_val: str) -> str:
    # The header can be any value, we only allow String.
    if not isinstance(header_val, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Authorization header is not a valid UTF-8 string.",
        )
    # The Authentication header is a Bearer token, so we need to extract the token from it.
    if not header_val.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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


async def get_username_from_token(token: str, rest_url: str, logger=None) -> str:
    """
    Calls the token-check endpoint at <rest_url>/api/freva-nextgen/auth/v2/systemuser
    and returns the username (pw_name).
    """
    log = logger or configure_logging(__name__)

    path = _normalize_systemuser_path(rest_url)
    url = f"{rest_url}{path}"
    log.debug("Token check URL: %s", url)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        # ServiceUnavailable on request error to vault/rest
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

    # parse JSON and extract username/detail
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
