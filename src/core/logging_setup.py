import os
import logging

_SILENCED = False

def configure_logging() -> None:
    """Basic console logging"""
    debug_mode = os.getenv("DEBUG_MODE", "off").lower() in ("1", "true", "on")
    level = "DEBUG" if debug_mode else "INFO"
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
