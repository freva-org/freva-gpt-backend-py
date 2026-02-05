from ansi2html import Ansi2HTMLConverter

from freva_gpt.core.logging_setup import configure_logging

logger = configure_logging(__name__)

# TODO: Frontend: sending html messages instead of stripping color codes
# Jupyter sends the stdout or stderr as a string containing ANSI escape sequences
# (color codes). We can send them as html messages.
conv = Ansi2HTMLConverter(inline=True)


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i:i+n]
