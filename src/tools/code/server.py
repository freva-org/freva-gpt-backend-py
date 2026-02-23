import os
import sys
import time
from contextvars import ContextVar
import threading
from queue import Empty
from typing import Dict, Any, Optional

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context

from jupyter_client import KernelManager

from src.core.logging_setup import configure_logging
from src.tools.header_gate import make_header_gate
from src.tools.server_auth import jwt_verifier
from src.tools.code.helpers import strip_ansi, sanitize_code, start_kernel, restart_kernel, shutdown_kernel
from src.tools.code.safety_check import check_code_safety

logger = configure_logging(__name__, named_log="code_server")

_disable_auth = os.getenv("FREVAGPT_MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}
mcp = FastMCP("code-interpreter-server", auth=None if _disable_auth else jwt_verifier)

KERNEL_REGISTRY: dict[str, KernelManager] = {} 
KERNEL_LOCKS: dict[str, threading.Lock] = {}
KERNEL_LOCKS_GUARD = threading.Lock()
# TODO: remove kernel from registry when session is closed


# ── Config ───────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = int(os.getenv("FREVAGPT_MCP_REQUEST_TIMEOUT_SEC", "300"))

# We leave 5 seconds buffer so server responds before client timeout
EXEC_TIMEOUT = max(1, REQUEST_TIMEOUT - 580)

IOPUB_DRAIN_AFTER_REPLY: float = 0.25
IOPUB_POLL = 0.1
SHELL_POLL = 0.1

# Recovery tuning
MAX_RECOVERY_RETRIES = 1  # restart+retry once

# ── Execution helpers ─────────────────────────────────────────────────────────

def _get_sid_lock(sid: str) -> threading.Lock:
    # Make lock creation thread-safe
    with KERNEL_LOCKS_GUARD:
        lock = KERNEL_LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            KERNEL_LOCKS[sid] = lock
        return lock


def _current_sid() -> str:
    ctx = get_context()
    s_id = getattr(ctx, "session_id", "")
    logger.info(f"Current session id:{s_id}")
    return s_id


def _get_or_start_kernel(sid: str, cwd_str: str) -> KernelManager:
    km = KERNEL_REGISTRY.get(sid)

    # Check existing kernel state
    if km is not None and not km.is_alive():
        # Dead kernel, discard it
        logger.warning("Kernel for sid=%s is dead; restarting", sid)
        shutdown_kernel(km)
        KERNEL_REGISTRY.pop(sid, None) # discard
        km = None
    elif km and km.is_alive():
        # Report alive kernel
        logger.warning("Kernel for sid=%s is alive and ready", sid)

    if km is None:
        logger.info("Starting new kernel for sid=%s", sid)
        # We preserve the env variables set in Dockerfile
        km = start_kernel(cwd_str)
        KERNEL_REGISTRY[sid] = km # register
    return km


def _drain_iopub(kc, max_msgs=50):
    for _ in range(max_msgs):
        try:
            kc.get_iopub_msg(timeout=0.01)
        except Empty:
            break


def _run_cell(sid: str, code: str) -> dict:
    working_dir = _get_cwd() or os.getcwd()
    km = _get_or_start_kernel(sid, cwd_str=working_dir)
    kc = km.client()
    kc.start_channels()

    try:
        _drain_iopub(kc) # removes stale queued messages from earlier runs
        msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)

        stdout_parts, stderr_parts, display_data, result_repr, error = [], [], [], None, None
        # There could be display_data that is sent with an id and these can be updated later using msg_type="update_display_data". 
        # For these, we keep only the last updated version.
        display_data_dict = {} 

        start = time.time()
        dealine = start + EXEC_TIMEOUT

        got_any_for_msg = False

        while time.time() < deadline:
            # Since Jupyter kernel runs asynchronously, it streams outputs, errors, and state messages while it executes the code.
            # We loop to collect them in real time until the status is "idle".
            try:
                # short-poll messages
                msg = kc.get_iopub_msg(timeout=1)
            except Empty:
                continue

            content = msg.get("content", {})

            # We check if the msg is from the cell we just executed, just in case there are idle cells still emitting.
            # old/stale/background messages vs current cell
            parent_id = (msg.get("parent_header") or {}).get("msg_id")
            msg_type = (msg.get("header") or {}).get("msg_type")

            if parent_id == msg_id:
                got_any_for_msg = True # we saw a message associated with this execution

            if msg_type == "status" and content.get("execution_state") == "idle":
                if parent_id == msg_id or got_any_for_msg:
                    break
                else:
                    continue

            # Ignore unrelated messages except for idle
            if parent_id != msg_id:
                continue

            if msg_type == "stream":
                (stdout_parts if content.get("name", "") == "stdout" else stderr_parts).append(
                    content.get("text", "")
                    )
            elif msg_type in ("display_data", "update_display_data"): 
                # Jupyter also returns rich outputs (image/png, text/html, etc.)
                display_id = content.get("transient", {}).get("display_id", "")
                if display_id: 
                    display_data_dict[display_id] = content.get("data", {})
                else: 
                    display_data.append(content.get("data", {}))
            elif msg_type == "execute_result":
                result_repr = content.get("data", {}).get("text/plain")
            elif msg_type == "error":  
                # Present only if an exception occurred. We record non-exception in stderr
                tb = "\n".join(content.get("traceback", []))
                error = tb or f"{content.get('ename')}: {content.get('evalue')}"

        else:
            # deadline exceeded (hard limit)
            try:
                km.interrupt_kernel()
            except Exception:
                logger.exception("Failed to interrupt kernel after timeout")
            raise TimeoutError(f"Execution exceeded {EXEC_TIMEOUT}s")

        # If we got any updated display in dict, we append them to the list.
        # Here, we are sending a list of unique output
        if display_data_dict:
            display_data.extend(list(display_data_dict.values()))

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
    
    safe, violation = check_code_safety(code)
    if safe:
        try:
            lock = _get_sid_lock(sid) 
            # Allowing only one _run_cell() at a time per sid 
            with lock: 
                sanitized_code = sanitize_code(code)
                out = _run_cell(sid, sanitized_code) 
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
            logger.exception("code_interpreter: unhandled execution error")
            return {
                "stdout": "",
                "stderr": "",
                "result_repr": "",
                "display_data": [],
                "error": msg,
            }
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


if __name__ == "__main__":
    # Configure Streamable HTTP transport 
    host = os.getenv("FREVAGPT_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("FREVAGPT_MCP_PORT", "8051"))
    path = os.getenv("FREVAGPT_MCP_PATH", "/mcp")  # standard path

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
    uvicorn.run(wrapped_app, host=host, port=port, ws="websockets-sansio",)
