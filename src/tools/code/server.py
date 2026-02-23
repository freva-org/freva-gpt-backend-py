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
from src.tools.code.helpers import (
    strip_ansi, sanitize_code, 
    start_kernel, restart_kernel, 
    shutdown_kernel, should_restart_after
)
from src.tools.code.safety_check import check_code_safety

logger = configure_logging(__name__, named_log="code_server")

_disable_auth = os.getenv("FREVAGPT_MCP_DISABLE_AUTH", "0").lower() in {"1","true","yes"}
mcp = FastMCP("code-interpreter-server", auth=None if _disable_auth else jwt_verifier)

# ── Kernel persistence ───────────────────────────────────────────────────────
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
MAX_RECOVERY_RETRIES = 1  # extra fresh-client attempt (no restart)

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
        logger.warning("Kernel for sid=%s is alive", sid)

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


def _run_shell(kc, code: str) -> dict:
    """
    Execute `code` on KernelClient `kc` and collect outputs.
    Completion is driven by SHELL execute_reply for msg_id.
    IOPub is used to collect stdout/stderr/rich outputs/errors.
    """
    msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)

    stdout_parts, stderr_parts, display_data, result_repr, error = [], [], [], None, None
    # There could be display_data that is sent with an id and these can be updated later using msg_type="update_display_data". 
    # For these, we keep only the last updated version.
    display_data_by_id = {} 

    start = time.time()
    deadline = start + EXEC_TIMEOUT

    got_shell_reply = False # authoritative “execution finished” signal
    shell_status: Optional[str] = None  # "ok" or "error"
    
    def handle_iopub(msg: Dict[str, Any]) -> None:
        nonlocal error, result_repr
        msg_type = (msg.get("header") or {}).get("msg_type")
        content = msg.get("content") or {}
        logger.debug(f"Message: {msg}")

        if msg_type == "stream":
            name = content.get("name", "")
            text = content.get("text", "")
            if name == "stdout":
                stdout_parts.append(text)
            else:
                stderr_parts.append(text)
            return

        if msg_type in ("display_data", "update_display_data"):
            # Jupyter also returns rich outputs (image/png, text/html, etc.)
            display_id = content.get("transient", {}).get("display_id", "")
            data = content.get("data") or {}
            if display_id:
                display_data_by_id[display_id] = data
            else:
                display_data.append(data)
            return

        if msg_type == "execute_result":
            result_repr = content.get("data", {}).get("text/plain")
            return

        if msg_type == "error":
            # Present only if an exception occurred. We record non-exception in stderr
            tb = "\n".join(content.get("traceback", []))
            error = tb or f"{content.get('ename')}: {content.get('evalue')}"
            return

    while time.time() < deadline:
        # 1) shell reply
        if not got_shell_reply:
            try:
                shell = kc.get_shell_msg(timeout=SHELL_POLL)
                parent = shell.get("parent_header") or {}
                if parent.get("msg_id") == msg_id:
                    got_shell_reply = True
                    shell_content = shell.get("content") or {}
                    shell_status = shell_content.get("status")
            except Empty:
                pass

        # 2) iopub outputs
        try: # short-poll iopub messages
            # Since Jupyter kernel runs asynchronously, it streams outputs, errors, 
            # and state messages while it executes the code.
            # We loop to collect them in real time until the status is "idle".
            io = kc.get_iopub_msg(timeout=IOPUB_POLL)
        except Empty:
            io = None

        if io is not None:
            parent = io.get("parent_header") or {}
            if parent.get("msg_id") == msg_id:
                handle_iopub(io)

        if got_shell_reply:
            # Once shell reply arrived, we stop the main loop. Execution is finished.
            break
    else:
        # deadline exceeded (hard limit)
        raise TimeoutError(f"Execution exceeded {EXEC_TIMEOUT}s")

    # We check iopub queue for a short period after the shell confirmed execution is finished
    drain_deadline = time.time() + max(0.0, IOPUB_DRAIN_AFTER_REPLY)
    while time.time() < drain_deadline:
        try:
            io = kc.get_iopub_msg(timeout=0.05)
        except Empty:
            # stop early if the queue is empty
            break
        parent = io.get("parent_header") or {}
        if parent.get("msg_id") != msg_id:
            continue
        handle_iopub(io)

    # If we got any updated display in dict, we append them to the list.
    # Here, we are sending a list of unique output
    if display_data_by_id:
        display_data.extend(list(display_data_by_id.values()))

    if shell_status == "error" and not error:
        error = "Execution failed (kernel reported error, but no traceback captured)."

    return {
        "stdout": strip_ansi("".join(stdout_parts)),
        "stderr": strip_ansi("".join(stderr_parts)),
        "result_repr": result_repr or "",
        "display_data": display_data, 
        "error": strip_ansi(error) if error else "",
    }


def _execute_code(sid: str, code: str) -> dict:
    """Execution wrapper with recovery"""
    working_dir = _get_cwd() or os.getcwd()
    km = _get_or_start_kernel(sid, cwd_str=working_dir)

    def _attempt_once() -> Dict[str, Any]:
        """ 
        Single execution attempt against the current kernel, 
        with clean channel lifecycle 
        """
        kc = km.client()
        kc.start_channels()
        try:
            _drain_iopub(kc) # removes stale queued messages from earlier runs
            out = _run_shell(kc, code)
            return out
        finally:
            kc.stop_channels()

    last_exc: Exception | None = None

    for attempt in range(MAX_RECOVERY_RETRIES + 1):
        try:
            out = _attempt_once()
            return out

        except TimeoutError as e:
            # execution exceeded wall-clock timeout -> interrupt kernel
            logger.warning("Execution timeout (sid=%s): %s", sid, e)
            try:
                km.interrupt_kernel()
            except Exception:
                logger.exception("Failed to interrupt kernel after timeout")
            raise

        except Exception as e:
            # A) KernelClient / channel / ZMQ state is bad (kernel may still be fine)
            # B) Kernel is alive but unresponsive (kernel is effectively broken)
            logger.warning("Kernel/channel failure (sid=%s, attempt=%d/%d): %s",
                            sid, attempt + 1, MAX_RECOVERY_RETRIES + 1, e)
            last_exc = e
            # retry with a new client
            continue

    else:
        # Here, fresh-client attempts failed => kernel likely wedged/unresponsive.
        # IMPORTANT: we do not restart silently (persistence contract).
        # Another option: restart kernel and re-attempt, return a warning to client
        # For now shutdown the kernel and warn the client
        shutdown_kernel(km)
        KERNEL_REGISTRY.pop(sid, None)
        root = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown"
        raise RuntimeError(
            "KERNEL_RESTARTED: execution kernel became unresponsive and was restarted.\n"
            "PERSISTENCE_LOST: session state/variables were reset.\n"
            "ACTION_REQUIRED: re-send required setup code (and any prior tool-call context) in the next request.\n"
            f"DETAILS: {root}"
        )


@mcp.tool()
def code_interpreter(code: str) -> dict:
    """
    Execute Python in a Jupyter-like IPython Kernel.
    Returns a structured dict with all outputs (stdout, stderr, result_rep, display_data, error)
    """
    sid = _current_sid()
    if not sid:
        raise RuntimeError("Missing Mcp-Session-Id")
    
    logger.info(f"Session id:{sid}\nKernel execution timeout:{EXEC_TIMEOUT}")
    logger.info(f"Input code:'{code}'")
    
    safe, violation = check_code_safety(code)
    if safe:
        logger.info(f"Code block is safe to execute..")
        lock = _get_sid_lock(sid) 
        # Allowing only one _execute_code() at a time per sid 
        with lock: 
            sanitized_code = sanitize_code(code)
            try:
                out = _execute_code(sid, sanitized_code) 
                if should_restart_after(sanitized_code):
                    # Check if exit() / quit() is present in the code block
                    # If so, discard kernel 
                    logger.warning("exit()/quit() detected; discarding kernel for sid=%s", sid)
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
