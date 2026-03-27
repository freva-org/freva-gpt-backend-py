import os, sys
import threading
from queue import Empty

from jupyter_client import KernelManager

from src.core.logging_setup import configure_logging

logger = configure_logging(__name__, named_log="code_server")


# ── Kernel persistence ───────────────────────────────────────────────────────

KERNEL_REGISTRY: dict[str, KernelManager] = {} 
KERNEL_LOCKS: dict[str, threading.Lock] = {}
KERNEL_LOCKS_GUARD = threading.Lock()
# TODO use sid locks not single guard

# ── Kernel lifecycle ─────────────────────────────────────────────────────────

def _kernel_ready_handshake(km: KernelManager, timeout: int = 10) -> None:
    kc = km.client()
    kc.start_channels()
    try:
        kc.wait_for_ready(timeout=timeout)
    finally:
        kc.stop_channels()


def start_kernel(cwd_str: str) -> KernelManager:
    env = os.environ.copy()
    km = KernelManager()
    km.kernel_cmd = [sys.executable, "-m", "ipykernel", "-f", "{connection_file}"]
    km.start_kernel(env=env, cwd=cwd_str)
    return km


def restart_kernel(km: KernelManager) -> None:
    km.restart_kernel(now=True)
    _kernel_ready_handshake(km, timeout=10)


def shutdown_kernel(km: KernelManager) -> None:
    try:
        km.shutdown_kernel(now=True)
    except Exception:
        logger.exception("Failed to shutdown dead kernel cleanly")


def get_or_start_kernel(sid: str, cwd_str: str) -> KernelManager:
    km = KERNEL_REGISTRY.get(sid)

    # Check existing kernel state
    if km is not None and not km.is_alive():
        # Dead kernel, discard it
        # NOTE: This restart may break persistance, it is not handled
        # TODO: maybe we should have a code history registry?
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
        try:
            _kernel_ready_handshake(km, timeout=10)
        except Exception:
            KERNEL_REGISTRY.pop(sid, None)
            shutdown_kernel(km)
            raise
    return km

# ── Drain stale messages ─────────────────────────────────────────────────────

def drain_stale_messages(kc, max_msgs: int = 100):
    # best effort to avoid stale messages from earlier runs
    _drain_iopub(kc, max_msgs)
    _drain_shell(kc, max_msgs) 
    _drain_control(kc, max_msgs)


def _drain_iopub(kc, max_msgs: int = 100):
    for _ in range(max_msgs):
        try:
            kc.get_iopub_msg(timeout=0.01)
        except Empty:
            break


def _drain_shell(kc, max_msgs: int = 100) -> None:
    """Drain any pending shell-channel messages."""
    for _ in range(max_msgs):
        try:
            kc.get_shell_msg(timeout=0.01)
        except Empty:
            break
        except Exception:
            break


def _drain_control(kc, max_msgs: int = 100) -> None:
    """Drain any pending control-channel messages."""
    for _ in range(max_msgs):
        try:
            kc.get_control_msg(timeout=0.01)
        except Empty:
            break
        except Exception:
            break
