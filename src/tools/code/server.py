# ── Performance metrics ──────────────────────────────────────────────────────────

from prometheus_client import Gauge, Histogram
import threading
import os

MCP_CODE_IN_PROGRESS = Gauge("mcp_code_in_progress", "Active tool executions in MCP code server")

MCP_CODE_EXEC_SECONDS = Histogram(
    "mcp_code_exec_seconds",
    "Time spent inside code_interpreter execution (server-side)",
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 60),
)

# ─────────────────────────────────────────────────────────────────────────────────

import time
from contextvars import ContextVar

from fastmcp import FastMCP

from src.core.logging_setup import configure_logging
from src.tools.asgi_wrapper import wrap_asgi_app
from src.tools.code.helpers import (
    sanitize_code, shutdown_kernel, should_restart_after
)
from src.tools.code.code_execution import (
    current_sid, get_sid_lock,
    execute_code, KERNEL_REGISTRY,
)
from src.tools.code.safety_check import check_code_safety

logger = configure_logging(__name__, named_log="code_server")

mcp = FastMCP("code-interpreter-server")


# ── App ───────────────────────────────────────────────────────────────────

# Per-request header context
CODE_INTERPRETER_CWD_HDR = "working-dir"
cwd_ctx: ContextVar[str | None] = ContextVar("cwd_ctx", default=None)

HOST = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("FREVAGPT_MCP_PORT", "8051"))
PATH = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path

# Configure Streamable HTTP transport
logger.info("Starting code-interpreter MCP server on %s:%s%s", HOST, PORT, PATH)


# Start the MCP server using Streamable HTTP transport
app = wrap_asgi_app(
    mcp.http_app(),
    ctx_list=[cwd_ctx],
    header_name_list=[CODE_INTERPRETER_CWD_HDR],
    logger=logger,
    mcp_path=PATH,
)


# ── MCP tool ───────────────────────────────────────────────────────────────────

@mcp.tool()
def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    sid = current_sid()
    if not sid:
        raise RuntimeError("Missing Mcp-Session-Id")

    logger.debug(f"Session id:{sid}\nKernel execution timeout:{EXEC_TIMEOUT}")
    logger.debug(f"Input code:'{code}'")

    violation = check_code_safety(code)
    if violation is None:
        logger.info("Code block is safe to execute..")
        lock = get_sid_lock(sid)
        # Allowing only one _execute_code() at a time per sid
        with lock:
            sanitized_code = sanitize_code(code)
            MCP_CODE_IN_PROGRESS.inc()
            t0 = time.perf_counter()
            try:
                out = execute_code(sid, sanitized_code)
                if should_restart_after(sanitized_code):
                    # Check if exit() / quit() is present in the code block
                    # If so, discard kernel
                    logger.warning(
                        "exit()/quit() detected; discarding kernel for sid=%s", sid
                    )
                    km = KERNEL_REGISTRY.get(sid)
                    if km is not None:
                        shutdown_kernel(km)
                    KERNEL_REGISTRY.pop(sid, None)
                return out
            except TimeoutError as e:
                msg = f"Execution failed: {e}"
                logger.exception("code_interpreter: execution timeout")
                return {
                    "stdout": "",
                    "stderr": "",
                    "result_repr": "",
                    "display_data": [],
                    "error": msg,
                }
            except Exception as e:
                msg = f"Execution failed: {type(e).__name__}: {e}"
                logger.exception("code_interpreter: execution error")
                return {
                    "stdout": "",
                    "stderr": "",
                    "result_repr": "",
                    "display_data": [],
                    "error": msg,
                }
            finally:
                MCP_CODE_EXEC_SECONDS.observe(time.perf_counter() - t0)
                MCP_CODE_IN_PROGRESS.dec()
    else:
        msg = (
            f"Code execution blocked by safety rule '{violation.rule_id}': "
            f"{violation.description} (matched: {violation.match!r})"
        )
        logger.warning(msg)
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }
