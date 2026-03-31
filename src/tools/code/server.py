import os
import time
from contextvars import ContextVar

from fastmcp import FastMCP

from src.core.logging_setup import configure_logging
from src.tools.header_gate import make_header_gate
from .code_execution import (
    current_sid, current_request_id, get_sid_lock, 
    execute_code, EXEC_TIMEOUT, cleanup_mcp_session
)
from .kernels import shutdown_kernel, KERNEL_REGISTRY
from .active_requests import register_request, cancel_request, unregister_request
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
logger.info("Starting code-interpreter MCP server on %s:%s%s",
            HOST, PORT, PATH)


# Start the MCP server using Streamable HTTP transport
app = make_header_gate(
    mcp.http_app(),
    ctx_list=[cwd_ctx],
    header_name_list=[CODE_INTERPRETER_CWD_HDR],
    logger=logger,       
    mcp_path=PATH,  
    on_session_close=cleanup_mcp_session,
    on_cancel_request=cancel_request,
)

def get_cwd():
    cwd = cwd_ctx.get()
    if not cwd:
        logger.warning(f"Missing required header '{CODE_INTERPRETER_CWD_HDR}'! "\
                       "Not setting CWD for code server, this MAY result in errors "\
                       "when the code interpreter saves data.")
        return
    else:
        return cwd
    

@mcp.tool()
def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    working_dir = get_cwd() or os.getcwd()
    sid = current_sid()
    request_id = current_request_id()

    if not sid:
        raise RuntimeError("Missing Mcp-Session-Id")
    if not request_id:
        raise RuntimeError("Missing MCP request id")
    
    logger.debug(f"Session id:{sid}\nRequest id:{request_id}\nKernel execution timeout:{EXEC_TIMEOUT}")
    stripped_code = code.replace("\n", "; ")
    logger.debug(f"Input code: {stripped_code}")
    
    violation = check_code_safety(code)

    if violation is None:
        logger.info("Code block is safe to execute..")
        lock = get_sid_lock(sid) 
        req = register_request(request_id, sid)

        try:
            with lock:
                if req.cancelled.is_set():
                    return {
                        "stdout": "",
                        "stderr": "",
                        "result_repr": "",
                        "display_data": [],
                        "error": "Execution cancelled by client",
                    }

                sanitized_code = sanitize_code(code)

                try:
                    out = execute_code(
                        sid=sid, 
                        code=sanitized_code, 
                        working_dir=working_dir, 
                        cancel_event=req.cancelled,
                        active_request=req,
                    )

                    if should_restart_after(sanitized_code):
                        logger.warning("exit()/quit() detected; discarding kernel for sid=%s", sid)
                        km = KERNEL_REGISTRY.get(sid)
                        if km is not None:
                            shutdown_kernel(km)
                        KERNEL_REGISTRY.pop(sid, None)

                    return out

                except InterruptedError:
                    logger.info("code_interpreter: execution cancelled for sid=%s request_id=%s", sid, request_id)
                    return {
                        "stdout": "",
                        "stderr": "",
                        "result_repr": "",
                        "display_data": [],
                        "error": "Execution cancelled by client",
                    }

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
            unregister_request(request_id)    
    else:
        msg = f"Code execution blocked by safety rule '{violation.rule_id}': " \
            f"{violation.description} (matched: {violation.match!r})"
        logger.warning(msg)
        return {
            "stdout": "",
            "stderr": "",
            "result_repr": "",
            "display_data": [],
            "error": msg,
        }
