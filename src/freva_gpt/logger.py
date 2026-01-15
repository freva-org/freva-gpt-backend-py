"""Definition of the central logging system."""

import logging
import os
import sysconfig
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import platformdirs
import rich.logging
from rich.console import Console

from .core.settings import ENV_PREFIX


def _get_logdir() -> Path:
    """Define the logging dir."""
    prefix = ENV_PREFIX.lower()
    env_dir = os.environ.get(f"{ENV_PREFIX}_LOGDIR")
    if env_dir:
        env_dir = Path(env_dir)
        env_dir.mkdir(exist_ok=True, parents=True)
        return env_dir
    for log_dir in (
        Path("/var/lib/{prefix.lower()}"),
        Path(sysconfig.get_paths()["data"]) / "var" / "lib" / prefix.lower(),
    ):
        if os.access(log_dir, os.W_OK):
            log_dir.mkdir(exist_ok=True, parents=True)
            return log_dir
        return platformdirs.user_log_path(prefix.lower(), ensure_exists=True)


THIS_NAME: str = ENV_PREFIX.lower()
BASE_LEVEL: int = int(os.getenv(f"{ENV_PREFIX}_LOG_LEVEL", str(logging.ERROR)))
logfmt = "%(name)s - %(message)s"
datefmt = "%Y-%m-%dT%H:%M"
logger_format = logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt
)
logger_file_handle = RotatingFileHandler(
    _get_logdir() / f"{THIS_NAME}.log",
    mode="a",
    maxBytes=5 * 1024**2,
    backupCount=2,
    encoding="utf-8",
    delay=False,
)
logger_file_handle.setFormatter(logger_format)
logger_file_handle.setLevel(BASE_LEVEL)
logger_stream_handle = rich.logging.RichHandler(
    rich_tracebacks=True,
    show_path=True,
    console=Console(
        soft_wrap=False,
        force_jupyter=False,
        stderr=True,
    ),
)
logger_stream_handle.setLevel(BASE_LEVEL)
logger = logging.getLogger(THIS_NAME)
logger.setLevel(BASE_LEVEL)


logging.basicConfig(
    level=BASE_LEVEL,
    format=logfmt,
    datefmt=datefmt,
    handlers=[logger_file_handle, logger_stream_handle],
)


def reset_loggers(level: Optional[int] = None) -> None:
    """Unify all loggers that we have currently aboard."""
    level = BASE_LEVEL if level is None else level
    logger.setLevel(level)
    for name in logging.root.manager.loggerDict.keys():
        logging.getLogger(name).handlers = [
            logger_file_handle,
            logger_stream_handle,
        ]
        logging.getLogger(name).propagate = True
        logging.getLogger(name).level = level


def get_level_from_verbosity(verbosity: int) -> int:
    """Calculate the log level from a verbosity."""
    return max(BASE_LEVEL - 10 * verbosity, -1)


def apply_verbosity(
    level: Optional[int] = None, suffix: Optional[str] = None
) -> int:
    """Set the logging level of the handlers to a certain level."""
    level = logger.level if level is None else level
    old_level = logger.level
    level = get_level_from_verbosity(level)
    reset_loggers(level)
    return old_level


reset_loggers()
