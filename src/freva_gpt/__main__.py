"""Module call of the cli."""

import sys

from .cli import cli_app

cli_app(sys.argv[1:])
