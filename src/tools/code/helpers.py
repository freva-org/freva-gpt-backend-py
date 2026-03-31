import re


def strip_ansi(text: str) -> str:
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


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
        out = f"import xarray as xr\nxr.set_options(display_style='text')\n{out}"

    # Comment out plt.close() calls
    # Matches "plt.close()" possibly with whitespace before/after
    out = re.sub(
        r"(?m)^\s*(plt\.close\s*\(\s*\))", r"# \1  # commented out by sanitizer", out
    )
    return out

# ── exit() / quit() handling ────────────────────────────────────────────────

EXIT_RE = re.compile(r"(?m)^\s*(exit|quit)\s*\(\s*\)\s*(#.*)?$")


def should_restart_after(code: str) -> bool:
    return bool(EXIT_RE.search(code))
