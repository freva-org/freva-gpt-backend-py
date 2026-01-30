import logging
import os
import sys
from contextvars import ContextVar

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from jupyter_client import KernelManager
from freva_gpt.core.logging_setup import configure_logging

from freva_gpt.tools.header_gate import make_header_gate
from freva_gpt.tools.server_auth import jwt_verifier
from freva_gpt.tools.code.helpers import (
    code_is_likely_safe,
    sanitize_code,
    strip_ansi,
)

logger = configure_logging(__name__, named_log="code_server")

_disable_auth = os.getenv("FREVAGPT_MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}
mcp = FastMCP("code-interpreter-server", auth=None if _disable_auth else jwt_verifier)

_KERNEL_REGISTRY: dict[str, KernelManager] = {} 
# TODO: remove kernel from registry when session is closed

# ── Config ───────────────────────────────────────────────────────────────────
EXEC_TIMEOUT = int(os.getenv("MCP_EXEC_TIMEOUT_SEC", "300"))  # soft guard in case the kernel hangs or runs forever

# ── Header helpers ────────────────────────────────────────────────────────────
# Per-request header context
CODE_INTERPRETER_CWD_HDR = "working-dir"
cwd_ctx: ContextVar[str | None] = ContextVar("cwd_ctx", default=None)

    
def _get_cwd():
    cwd = cwd_ctx.get()
    if not cwd:
        logger.warning(f"Missing required header '{CODE_INTERPRETER_CWD_HDR}'! "\
                       "Not setting CWD for code server, this MAY result in errors when the code interpreter saves data.")
        return
    else:
        return cwd
    
# ── Execution helpers ─────────────────────────────────────────────────────────

def _current_sid() -> str:
    ctx = get_context()
    return (getattr(ctx, "session_id"), "")

def _get_or_start_kernel(sid: str, cwd_str: str) -> KernelManager:
    km = _KERNEL_REGISTRY.get(sid)
    if km is None:
        # We preserve the env variables set in Dockerfile
        env = os.environ.copy()
        km = KernelManager()
        km.kernel_cmd = [sys.executable, "-m", "ipykernel", "-f", "{connection_file}"]  # Otherwise "No such kernel named python3"
        km.start_kernel(env=env, cwd=cwd_str)
        _KERNEL_REGISTRY[sid] = km
    return km

def _run_cell(sid: str, code: str) -> dict:
    working_dir = _get_cwd() or os.getcwd()
    km = _get_or_start_kernel(sid, cwd_str=working_dir)
    kc = km.client()
    kc.start_channels()
    try:
        msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
        stdout_parts, stderr_parts, display_data, result_repr, error = [], [], [], None, None
        # There could be display_data that is sent with an id and these can be updated later using msg_type="update_display_data". 
        # For these, we keep only the last updated version.
        display_data_dict = {} 

        # Since Jupyter kernel runs asynchronously, it streams outputs, errors, and state messages while it executes the code.
        # We loop to collect them in real time until the status is "idle".
        while True:
            msg = kc.get_iopub_msg(timeout=EXEC_TIMEOUT)
            # We check if the msg is from the cell we just executed, just in case there are idle cells still emitting.
            # old/stale/background messages vs current cell
            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            if msg_type == "status" and msg["content"]["execution_state"] == "idle":
                break
            elif msg_type == "stream":
                (stdout_parts if msg["content"]["name"] == "stdout" else stderr_parts).append(msg["content"]["text"])
            elif msg_type in ("display_data", "update_display_data"): # Jupyter also returns rich outputs (image/png, text/html, etc.)
                display_id = msg["content"].get("transient", {}).get("display_id", "")
                if display_id: 
                    display_data_dict.update({display_id: msg["content"].get("data", {})})
                else: 
                    display_data.append(msg["content"].get("data", {}))
            elif msg_type == "execute_result":
                result_repr = msg["content"].get("data", {}).get("text/plain")
            elif msg_type == "error":  # Present only if an exception occurred. We record non-exception in stderr
                tb = "\n".join(msg["content"].get("traceback", []))
                error = tb or f"{msg['content'].get('ename')}: {msg['content'].get('evalue')}"

        # If we got any updated display in dict, we append them to the list.
        # Here, we are sending a list of unique output
        if display_data_dict:
            display_data.append(list(display_data_dict.values()))

        return {
            "stdout": strip_ansi("".join(stdout_parts)),
            "stderr": strip_ansi("".join(stderr_parts)),
            "result_repr": result_repr if result_repr else "",
            "display_data": display_data, 
            "error": strip_ansi(error) if error else "",
        }
    finally:
        kc.stop_channels()

@mcp.tool()
def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    sid = _current_sid()
    if not sid:
        raise RuntimeError("Missing Mcp-Session-Id")
    if code_is_likely_safe(code):
        sanitized_code = sanitize_code(code)
        try:
            return _run_cell(sid, sanitized_code)
        except Exception as e:
            logger.exception("code_interpreter: unhandled execution error")
            raise Exception(f"Execution failed: {type(e).__name__}: {e}")
    else:
        logger.warning("Code is not executed due to potential safety concerns!")
        return 
        

if __name__ == "__main__":
    # Configure Streamable HTTP transport 
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8051"))
    path = os.getenv("MCP_PATH", "/mcp")  # standard path

    logger.info("Starting code-interpreter MCP server on %s:%s%s (auth=%s)",
                host, port, path, "off" if _disable_auth else "on")
    
    # Start the MCP server using Streamable HTTP transport
    wrapped_app = make_header_gate(
        mcp.http_app(),
        ctx_list=[cwd_ctx],
        header_name_list=[CODE_INTERPRETER_CWD_HDR],
        logger=logger,       
        mcp_path=path,  
    )

    import uvicorn
    uvicorn.run(wrapped_app, host=host, port=port)
