import threading

from dataclasses import dataclass

from src.tools.code.kernels import KERNEL_REGISTRY

from src.core.logging_setup import configure_logging

logger = configure_logging(__name__, named_log="code_server")

# ── Active request tracking ───────────────────────────────────────────────────

@dataclass
class ActiveRequest:
    sid: str
    request_id: str
    cancelled: threading.Event
    execute_sent: threading.Event


ACTIVE_REQUESTS: dict[str, ActiveRequest] = {}
ACTIVE_REQUESTS_GUARD = threading.Lock()


def register_request(request_id: str, sid: str) -> ActiveRequest:
    req = ActiveRequest(
        sid=sid,
        request_id=request_id,
        cancelled=threading.Event(),
        execute_sent=threading.Event(),
    )
    with ACTIVE_REQUESTS_GUARD:
        ACTIVE_REQUESTS[request_id] = req
    return req


def unregister_request(request_id: str) -> None:
    with ACTIVE_REQUESTS_GUARD:
        ACTIVE_REQUESTS.pop(request_id, None)


def get_request(request_id: str) -> ActiveRequest | None:
    with ACTIVE_REQUESTS_GUARD:
        return ACTIVE_REQUESTS.get(request_id)


def cancel_request(sid: str, request_id: str) -> None:
    req= get_request(request_id)
    if req is None:
        logger.info("Cancellation ignored: unknown request_id=%s", request_id)
        return

    if req.sid != sid:
        logger.warning(
            "Cancellation ignored: request_id=%s belongs to sid=%s, not sid=%s",
            request_id,
            req.sid,
            sid,
        )
        return

    if not req.cancelled.is_set():
        logger.info("Cancelling request_id=%s sid=%s", request_id, sid)
        req.cancelled.set()

    km = KERNEL_REGISTRY.get(sid)
    logger.info(
        "cancel_request lookup sid=%s request_id=%s km_found=%s registry_ids=%s",
        sid,
        request_id,
        km is not None,
        list(KERNEL_REGISTRY.keys()),
    )

    # IMPORTANT: Only interrupt if code execution was actually submitted to the kernel.
    # If cancel happens during startup / wait_for_ready, do not interrupt the kernel.
    if km is None:
        return

    if not req.execute_sent.is_set():
        logger.info(
            "Cancellation happened before kc.execute(); not interrupting kernel "
            "for sid=%s request_id=%s",
            sid,
            request_id,
        )
        return

    try:
        logger.info("Interrupting kernel for sid=%s request_id=%s", sid, request_id)
        km.interrupt_kernel()
    except Exception:
        logger.exception("Failed to interrupt kernel for sid=%s request_id=%s", sid, request_id)