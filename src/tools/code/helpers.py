import re

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


def code_is_likely_safe(code: str) -> bool:
    """
    Checks whether the given code passes basic safety checks.
    """

    # Patterns considered dangerous
    # Allowing file I/O, but blocking system, subprocess, and execution functions.
    dangerous_patterns = [
        "import os",
        "import sys",  # May be allowed in testing, but should be blocked in production
        "exec(",
        "eval(",
        "subprocess",
        "socket",
        "os.system",
        "shutil",
        "ctypes",
        "pickle",
        "__import__",
        "get_ipython",  # Access to IPython internals
    ]

    # Check for static string patterns first
    for pattern in dangerous_patterns:
        if pattern in code:
            logger.warning(f"The code contains a dangerous pattern: {pattern}")
            logger.debug(f"The code is: {code}")
            return False

    # Regex check for Jupyter magics and shell escapes
    # Matches lines starting with '%' or '!' (optionally with whitespace before)
    # In the future, we may introduce a white list of packages that is allowed to be installed by the user.
    magic_pattern = re.compile(r"(?m)^\s*[!%]")
    if magic_pattern.search(code):
        logger.warning("The code contains a Jupyter magic line or shell escape.")
        logger.debug(f"The code is: {code}")
        return False

    # Passed all checks
    return True
