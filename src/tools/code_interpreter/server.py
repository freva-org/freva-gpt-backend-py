import os
import sys
from io import StringIO
from typing import Optional

import requests
from IPython.core.interactiveshell import InteractiveShell

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token  # exposes parsed token in tools

from src.logging_setup import configure_logging

logger = configure_logging()

# ──────────────────────────────────────────────────────────────────────────────
# OAuth/OIDC → JWT Bearer verification (JWKS discovery)
# ──────────────────────────────────────────────────────────────────────────────
ISSUER   = os.getenv("OIDC_ISSUER",   "https://www.freva.dkrz.de/api/freva-nextgen/")
AUDIENCE = os.getenv("OIDC_AUDIENCE", "freva")
REQUIRED_SCOPES = [s.strip() for s in os.getenv("MCP_REQUIRED_SCOPES", "mcp:execute").split(",") if s.strip()]

disc = requests.get(ISSUER.rstrip("/") + "/.well-known/openid-configuration", timeout=10).json()
JWKS_URI = disc["jwks_uri"]

jwt_verifier = JWTVerifier(
    jwks_uri=JWKS_URI,
    issuer=ISSUER,
    audience=AUDIENCE,
)

# ──────────────────────────────────────────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────────────────────────────────────────
mcp = FastMCP("code-interpreter-server", auth=jwt_verifier)

# Single shared IPython shell (Jupyter-like)
_shell = InteractiveShell.instance()

# Hard limits to avoid memory blowups in logs / responses
MAX_STD_CAP = int(os.getenv("MCP_MAX_STD_CAP", "200_000"))  # ~200 KB
TRUNC_MSG = "\n\n[output truncated]\n"

def _truncate(s: str, cap: int = MAX_STD_CAP) -> str:
    if len(s) <= cap:
        return s
    return s[:cap] + TRUNC_MSG

def _run_code(code: str) -> str:
    """Execute Python code in a persistent IPython shell and capture stdout/stderr/result."""
    old_out, old_err = sys.stdout, sys.stderr
    out_buf, err_buf = StringIO(), StringIO()
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        result = _shell.run_cell(code, store_history=False)
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    out = out_buf.getvalue()
    err = err_buf.getvalue()

    # Append repr of expression value if present
    if result and getattr(result, "result", None) is not None:
        if out and not out.endswith("\n"):
            out += "\n"
        out += repr(result.result)

    if err:
        return _truncate(f"Error:\n{err.strip()}\n{out.strip()}")
    if out.strip():
        return _truncate(out.strip())
    return "Code executed successfully with no output."

@mcp.tool()
def auth_health() -> dict:
    """
    Debug-only: returns server auth expectations and the parsed access token claims.
    Remove after debugging.
    """
    tok = get_access_token()
    return {
        "server_expectations": {
            "ISSUER_env": ISSUER,
            "AUDIENCE_env": AUDIENCE,
            "REQUIRED_SCOPES_env": REQUIRED_SCOPES,
        },
        "token": {
            "issuer": tok.issuer,
            "audience": tok.audience,       # may be str or list depending on provider
            "subject": tok.subject,
            "client_id": getattr(tok, "client_id", None),
            "scopes": tok.scopes,           # parsed scopes if available
            "expires_at": getattr(tok, "expires_at", None),
            "raw_claims_subset": {          # helpful peek; keep it minimal
                "iss": tok.issuer,
                "aud": tok.audience,
                "sub": tok.subject,
                "scope": getattr(tok, "scope", None),
            },
        },
    }


@mcp.tool()
def code_interpreter(code: str, require_scope: Optional[str] = None) -> str:
    """
    Execute Python in a Jupyter-like IPython context.

    Args:
        code: The Python code to execute.
        require_scope: Optional extra scope to enforce for this call (in addition to global REQUIRED_SCOPES).

    Returns:
        Captured stdout/stderr and last expression value (repr), truncated if too large.

    Security:
        - Requires a valid Bearer token (JWT) with audience={AUDIENCE} and issuer={ISSUER}.
        - Enforces global REQUIRED_SCOPES (env MCP_REQUIRED_SCOPES) and optional per-call `require_scope`.
    """
    # Fine-grained authorization: verify scopes from the validated token
    access_token = get_access_token()
    missing_global = [s for s in REQUIRED_SCOPES if s and s not in access_token.scopes]
    if missing_global:
        raise Exception(f"Missing required scopes: {', '.join(missing_global)}")
    if require_scope and require_scope not in access_token.scopes:
        raise Exception(f"Missing required scope: {require_scope}")

    try:
        return _run_code(code)
    except Exception as e:
        logger.exception("code_interpreter: unhandled execution error")
        raise Exception(f"Execution failed: {type(e).__name__}: {e}")

if __name__ == "__main__":
    # Streamable HTTP transport (recommended for scaling)
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8051"))
    path = os.getenv("MCP_PATH", "/mcp")  # standard mount path

    logger.info("Starting code-interpreter MCP server on %s:%s%s", host, port, path)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
        # tip: consider `stateless_http=True` if you want to scale horizontally with no sticky sessions
    )
