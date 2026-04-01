import os
import asyncio
from contextvars import ContextVar

from fastmcp import FastMCP

from src.core.logging_setup import configure_logging
from src.tools.header_gate import make_header_gate
from src.tools.active_requests import (
    ActiveRequest,
    RequestCancelled,
    current_ids,
    tracked_request,
)
from .code_execution import (
    get_sid_lock,
    execute_code,
    EXEC_TIMEOUT,
    cleanup_mcp_session,
    cancel_code_request,
)
from .kernels import shutdown_kernel, KERNEL_REGISTRY
from .helpers import sanitize_code, should_restart_after
from .safety_check import check_code_safety

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
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[cwd_ctx],
    header_name_list=[CODE_INTERPRETER_CWD_HDR],
    logger=logger,
    mcp_path=PATH,
    on_session_close=cleanup_mcp_session,
    on_cancel_request=cancel_code_request,
)


def get_cwd():
    cwd = cwd_ctx.get()
    if not cwd:
        logger.warning(
            f"Missing required header '{CODE_INTERPRETER_CWD_HDR}'! "
            "Not setting CWD for code server, this MAY result in errors "
            "when the code interpreter saves data."
        )
        return
    else:
        return cwd


def _run_code_request(
    sid: str, code: str, working_dir: str, req: ActiveRequest
) -> dict:
    lock = get_sid_lock(sid)
    with lock:
        if req.cancelled_thread.is_set():
            raise RequestCancelled("Execution cancelled by client")

        sanitized_code = sanitize_code(code)
        out = execute_code(
            session_id=sid,
            code=sanitized_code,
            working_dir=working_dir,
            cancel_event=req.cancelled_thread,
            active_request=req,
        )

        if should_restart_after(sanitized_code):
            logger.warning("exit()/quit() detected; discarding kernel for sid=%s", sid)
            km = KERNEL_REGISTRY.get(sid)
            if km is not None:
                shutdown_kernel(km)
            KERNEL_REGISTRY.pop(sid, None)

        return out


@mcp.tool()
async def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    working_dir = get_cwd() or os.getcwd()
    session_id, request_id = current_ids()

    logger.debug(
        f"Session id:{session_id}\nRequest id:{request_id}\nKernel execution timeout:{EXEC_TIMEOUT}"
    )
    stripped_code = code.replace("\n", "; ")
    logger.debug(f"Input code: {stripped_code}")

    violation = check_code_safety(code)

    if violation:
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

    logger.info("Code block is safe to execute..")

    try:
        async with tracked_request(session_id, request_id) as req:
            req.raise_if_cancelled()
            return await asyncio.to_thread(
                _run_code_request, session_id, code, working_dir, req
            )
    # NOTE: a future refactor to make the whole pipeline async would be good (including the kernel management).
    # But this is a good start and allows the use of existing sync code execution logic with minimal changes.

    except RequestCancelled:
        logger.info(
            f"code_interpreter: execution cancelled for sid={session_id} request_id={request_id}"
        )
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": "Execution cancelled by client",
        }

    except InterruptedError as e:
        logger.info(
            f"code_interpreter: execution interrupted unexpectedly for sid={session_id} request_id={request_id}"
        )
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": f"Execution interrupted unexpectedly {e}",
        }

    except TimeoutError as e:
        msg = f"Execution failed: {e}"
        logger.exception(f"code_interpreter: execution timeout {msg}")
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }

    except Exception as e:
        msg = f"Execution failed: {type(e).__name__}: {e}"
        logger.exception(f"code_interpreter: execution error {e}")
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }
