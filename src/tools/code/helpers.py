import re
import os, sys

from jupyter_client import KernelManager

from src.core.logging_setup import configure_logging

logger = configure_logging(__name__, named_log="code_server")


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def sanitize_code(code: str) -> str:
    """
    Sanitizes code for a headless server environment:
      - If 'matplotlib' or 'plt' is present, silence verbose matplotlib font manager logging.
      - If 'xarray' is present, switch its default display style to plain text
        to avoid HTML/CSS-heavy outputs.
    """
    out = code

    # Matplotlib backend selection and log silencing
    if ("matplotlib" in out) or ("plt" in out):
        to_add = (
            "import matplotlib\n"
            "import logging\n"
            "logging.getLogger('matplotlib.font_manager').disabled = True\n"
            "import matplotlib.pyplot as plt\n"
        )
        out = f"{to_add}{out}"

    # xarray text display (prepend so it runs before user code)
    if "xarray" in out:
        out = (
            "import xarray as xr\n"
            "xr.set_options(display_style='text')\n"
            f"{out}"
        )

    # Comment out plt.close() calls
    # Matches "plt.close()" possibly with whitespace before/after
    out = re.sub(r"(?m)^\s*(plt\.close\s*\(\s*\))", r"# \1  # commented out by sanitizer", out)
    return out


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
    _kernel_ready_handshake(km, timeout=10)
    return km


def restart_kernel(km: KernelManager) -> None:
    km.restart_kernel(now=True)
    _kernel_ready_handshake(km, timeout=10)


def shutdown_kernel(km: KernelManager) -> None:
    try:
        km.shutdown_kernel(now=True)
    except Exception:
        logger.exception("Failed to shutdown dead kernel cleanly")

# ── exit() / quit() handling ────────────────────────────────────────────────

EXIT_RE = re.compile(r"(?m)^\s*(exit|quit)\s*\(\s*\)\s*(#.*)?$")

def should_restart_after(code: str) -> bool:
    return bool(EXIT_RE.search(code))