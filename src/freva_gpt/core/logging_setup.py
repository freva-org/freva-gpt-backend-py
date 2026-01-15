import logging
import os

from freva_gpt.core.settings import get_settings

_SILENCED = False
settings = get_settings()

def configure_logging() -> None:
    """Basic console logging"""
    if settings.DEV_MODE:
        level = "DEBUG"
    else:
        level = "INFO"
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


def silence_logger():
    global _SILENCED
    if not _SILENCED:
        logging.disable(logging.CRITICAL)
        _SILENCED = True


def undo_silence_logger():
    global _SILENCED
    if _SILENCED:
        logging.disable(logging.NOTSET)
        _SILENCED = False
