# --- imports ---
import os
import sys
import logging
from io import StringIO
from typing import Optional

from IPython.core.interactiveshell import InteractiveShell
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

from src.core.logging_setup import configure_logging
from src.tools.server_auth import jwt_verifier, REQUIRED_SCOPES

configure_logging()
logger = logging.getLogger(__name__)

_disable_auth = os.getenv("MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}
mcp = FastMCP("code-interpreter-server", auth=None if _disable_auth else jwt_verifier)

_shell = InteractiveShell.instance()

MAX_STD_CAP = int(os.getenv("MCP_MAX_STD_CAP", "200_000"))
TRUNC_MSG = "\n\n[output truncated]\n"

EXEC_TIMEOUT = int(os.getenv("MCP_EXEC_TIMEOUT_SEC", "20"))  # soft guard

def _truncate(s: str, cap: int = MAX_STD_CAP) -> str:
    return s if len(s) <= cap else s[:cap] + TRUNC_MSG

def _run_code(code: str) -> str:
    # optional: POSIX-only soft timeout
    try:
        import signal
        def _raise_timeout(*_): raise TimeoutError(f"Execution exceeded {EXEC_TIMEOUT}s")
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.alarm(EXEC_TIMEOUT)
    except Exception:
        pass  # ignore on non-POSIX

    old_out, old_err = sys.stdout, sys.stderr
    out_buf, err_buf = StringIO(), StringIO()
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        result = _shell.run_cell(code, store_history=False)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        try:
            import signal; signal.alarm(0)
        except Exception:
            pass

    out = out_buf.getvalue()
    err = err_buf.getvalue()

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
def code_interpreter(code: str, require_scope: Optional[str] = None) -> str:
    """
    Execute Python in a Jupyter-like IPython context.
    Returns stdout/stderr + last expr repr (truncated).
    """
    # Skip scope checks if auth is disabled (local dev)
    if not _disable_auth:
        access_token = get_access_token()
        missing_global = [s for s in REQUIRED_SCOPES if s and s not in access_token.scopes]
        if missing_global:
            raise Exception(f"Missing required scopes: {', '.join(missing_global)}")
        if require_scope and require_scope not in access_token.scopes:
            raise Exception(f"Missing required scope: {require_scope}")

    try:
        return _run_code(code)
    except TimeoutError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("code_interpreter: unhandled execution error")
        raise Exception(f"Execution failed: {type(e).__name__}: {e}")

if __name__ == "__main__":
    # Streamable HTTP transport (recommended for scaling)
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8051"))
    path = os.getenv("MCP_PATH", "/mcp")  # standard path

    logger.info("Starting code-interpreter MCP server on %s:%s%s (auth=%s)",
                host, port, path, "off" if _disable_auth else "on")
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )
