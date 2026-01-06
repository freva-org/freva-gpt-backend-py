import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from ansi2html import Ansi2HTMLConverter

from freva_gpt.core.logging_setup import configure_logging

log = logging.getLogger(__name__)
configure_logging()


# TODO: Frontend: sending html messages instead of stripping color codes
# Jupyter sends the stdout or stderr as a string containing ANSI escape sequences
# (color codes). We can send them as html messages.
conv = Ansi2HTMLConverter(inline=True)


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────


def chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i : i + n]
