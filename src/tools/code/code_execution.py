import os
import threading
import time
from queue import Empty
from typing import Dict, Any, Optional

from fastmcp.server.dependencies import get_context
from contextvars import ContextVar

from src.tools.code.helpers import strip_ansi
from src.tools.code.kernels import (
    get_or_start_kernel, shutdown_kernel,
    KERNEL_LOCKS_GUARD, KERNEL_REGISTRY, KERNEL_LOCKS,
    drain_stale_messages
)
from src.tools.code.active_requests import ActiveRequest
from src.core.logging_setup import configure_logging

logger = configure_logging(__name__, named_log="code_server")

# ── Config ───────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = int(os.getenv("FREVAGPT_MCP_REQUEST_TIMEOUT_SEC", "600"))

# We leave 5 seconds buffer so server responds before client timeout
EXEC_TIMEOUT = max(1, REQUEST_TIMEOUT - 5)

logger.info("MCP Code-Server timeouts configured", extra={
    "request_timeout": REQUEST_TIMEOUT,
    "exec_timeout": EXEC_TIMEOUT,
})

IOPUB_DRAIN_AFTER_REPLY: float = 0.25
IOPUB_POLL = 0.1
SHELL_POLL = 0.1

# Recovery tuning
MAX_RECOVERY_RETRIES = 1  # extra fresh-client attempt (no restart)

# Per-request header context
CODE_INTERPRETER_CWD_HDR = "working-dir"
cwd_ctx: ContextVar[str | None] = ContextVar("cwd_ctx", default=None)
request_id_ctx: ContextVar[str | None] = ContextVar("request_id_ctx", default=None)


# ── Code execution ───────────────────────────────────────────────────────────

def cleanup_mcp_session(sid: str) -> None:
    """
    Best-effort cleanup for one MCP session.
    Removes the kernel and kernel lock from registry is session is closed 
    by MCP client
    """
    if not sid:
        return

    km = KERNEL_REGISTRY.pop(sid, None)
    if km is not None:
        logger.info("Cleaning up kernel for closed session sid=%s", sid)
        shutdown_kernel(km) 

    with KERNEL_LOCKS_GUARD:
        KERNEL_LOCKS.pop(sid, None)


def get_sid_lock(sid: str) -> threading.Lock:
    # Make lock creation thread-safe
    with KERNEL_LOCKS_GUARD:
        lock = KERNEL_LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            KERNEL_LOCKS[sid] = lock
        return lock


def current_sid() -> str:
    ctx = get_context()
    s_id = getattr(ctx, "session_id", "")
    logger.info(f"Current session id:{s_id}")
    return s_id


def current_request_id() -> str:
    ctx = get_context()
    req_id = getattr(ctx, "request_id", "")
    logger.info(f"Current request id:{req_id}")
    return req_id


def _run_shell(
    kc,
    code: str,
    cancel_event: threading.Event,
    active_request: ActiveRequest,
) -> dict:
    """
    Execute `code` on KernelClient `kc` and collect outputs.
    Completion is driven by SHELL execute_reply for msg_id.
    IOPub is used to collect stdout/stderr/rich outputs/errors.
    """
    if cancel_event.is_set():
        logger.info(
            "Request cancelled before kc.execute(); returning cancellation immediately "
            "for sid=%s request_id=%s",
            active_request.sid,
            active_request.request_id,
        )
        raise InterruptedError
    
    active_request.execute_sent.set()
    msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=False)
    stripped_code = code.replace("\n", "; ")
    logger.info(f"Started executing the code: {stripped_code}")
   
    stdout_parts, stderr_parts, display_data, result_repr, error = [], [], [], None, None
    # There could be display_data that is sent with an id and these can be updated later using msg_type="update_display_data". 
    # For these, we keep only the last updated version.
    display_data_by_id = {} 

    start = time.time()
    deadline = start + EXEC_TIMEOUT

    got_shell_reply = False # authoritative “execution finished” signal
    shell_status: Optional[str] = None  # "ok" or "error"

    cancelled = False
    cancel_deadline: Optional[float] = None
    saw_idle_for_msg = False

    # Give the interrupted execution enough time to actually unwind and emit
    # its terminal messages before we give up.
    CANCEL_FINALIZE_TIMEOUT = 10.0
    
    def handle_iopub(msg: Dict[str, Any]) -> None:
        nonlocal error, result_repr, saw_idle_for_msg
        msg_type = (msg.get("header") or {}).get("msg_type")
        content = msg.get("content") or {}

        if msg_type == "status":
            if content.get("execution_state") == "idle":
                saw_idle_for_msg = True
            return

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
        now = time.time()

        # Cancellation is signaled externally via cancel_request(), which both
        # sets cancel_event and calls km.interrupt_kernel()
        # Once we observe cancellation, switch into finalize mode but keep
        # reading from the SAME client / SAME msg_id until that interrupted
        # execution really finishes.
        if cancel_event.is_set() and not cancelled:
                cancelled = True
                cancel_deadline = now + CANCEL_FINALIZE_TIMEOUT

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

        if got_shell_reply and not cancelled:
            # If not cancelled: normal successful/error completion path:
            # once execute_reply arrives, the execution is terminal.
            break

        # Cancelled path: we do not abondon the cancelled client immediately, but keep waiting
        # for shell reply and idle message
        if cancelled and got_shell_reply:
            logger.info("Execution cancelled after interrupt finalized via shell reply")
            drain_stale_messages(kc)
            raise InterruptedError

        # Cancelled path: if interrupted execution never finalizes, fail explicitly.
        if cancelled and cancel_deadline is not None and now >= cancel_deadline:
            raise RuntimeError(
                "Interrupted execution did not finalize after kernel interrupt"
            )

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

    if cancelled:
        raise InterruptedError

    if shell_status == "error" and not error:
        error = "Execution failed (kernel reported error, but no traceback captured)."

    return {
        "stdout": strip_ansi("".join(stdout_parts)),
        "stderr": strip_ansi("".join(stderr_parts)),
        "result_repr": result_repr or "",
        "display_data": display_data, 
        "error": strip_ansi(error) if error else "",
    }


def execute_code(
    sid: str,
    code: str,
    working_dir,
    cancel_event: threading.Event,
    active_request: ActiveRequest,
) -> dict:
    """Execution wrapper with recovery"""
    if cancel_event.is_set():
        logger.info(
            "Request already cancelled before kernel startup for sid=%s request_id=%s",
            active_request.sid,
            active_request.request_id,
        )
        return InterruptedError
    
    km = get_or_start_kernel(sid, cwd_str=working_dir)

    def _attempt_once() -> Dict[str, Any]:
        """ 
        Single execution attempt against the current kernel, 
        with clean channel lifecycle 
        """
        kc = km.client()
        kc.start_channels()
        try:
            drain_stale_messages(kc)
            out = _run_shell(kc, code, cancel_event, active_request)
            return out
        finally:
            kc.stop_channels()

    last_exc: Exception | None = None

    for attempt in range(MAX_RECOVERY_RETRIES + 1):
        try:
            out = _attempt_once()
            return out

        except InterruptedError:
            logger.info("Execution cancelled (sid=%s)", sid)
            raise

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