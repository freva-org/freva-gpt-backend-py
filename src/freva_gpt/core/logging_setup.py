import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional

from freva_gpt.core.settings import get_settings

_SILENCED = False
_CONFIGURED = False
_THREAD_HANDLERS: Dict[str, RotatingFileHandler] = {}
_NAMED_HANDLERS: Dict[str, RotatingFileHandler] = {}

settings = get_settings()

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
MAIN_LOG = LOG_DIR / "app.log"
MAIN_MAX_BYTES = 5_000_000
MAIN_BACKUP_COUNT = 5
THREAD_MAX_BYTES = 1_000_000
THREAD_BACKUP_COUNT = 3

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [thread=%(thread_id)s user=%(user_id)s] %(message)s"
LOG_FORMATTER = logging.Formatter(LOG_FORMAT)


class ContextFilter(logging.Filter):
    """Ensures thread_id/user_id keys exist on log records."""

    def __init__(self, thread_id: Optional[str] = None, user_id: Optional[str] = None) -> None:
        super().__init__()
        self.thread_id = thread_id or "-"
        self.user_id = user_id or "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_id = getattr(record, "thread_id", self.thread_id) or "-"
        record.user_id = getattr(record, "user_id", self.user_id) or "-"
        return True


class ThreadFilter(ContextFilter):
    """Only allow records for the given thread_id to reach a handler."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(thread_id=thread_id)
        self.expected = thread_id or "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_id = getattr(record, "thread_id", self.thread_id) or "-"
        record.user_id = getattr(record, "user_id", self.user_id) or "-"
        return record.thread_id == self.expected


def _ensure_base_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if settings.DEV else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    base_filter = ContextFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(LOG_FORMATTER)
    stream_handler.addFilter(base_filter)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        MAIN_LOG,
        maxBytes=MAIN_MAX_BYTES,
        backupCount=MAIN_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(LOG_FORMATTER)
    file_handler.addFilter(base_filter)
    root.addHandler(file_handler)

    logging.getLogger("uvicorn").setLevel(logging.WARNING)

    _CONFIGURED = True


def _get_thread_handler(thread_id: str) -> RotatingFileHandler:
    if thread_id in _THREAD_HANDLERS:
        return _THREAD_HANDLERS[thread_id]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / f"{thread_id}.log",
        maxBytes=THREAD_MAX_BYTES,
        backupCount=THREAD_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # create file lazily on first emit
    )
    handler.setFormatter(LOG_FORMATTER)
    handler.addFilter(ThreadFilter(thread_id=thread_id))
    _THREAD_HANDLERS[thread_id] = handler
    return handler


def _get_named_handler(log_name: str) -> RotatingFileHandler:
    if log_name in _NAMED_HANDLERS:
        return _NAMED_HANDLERS[log_name]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / f"{log_name}.log",
        maxBytes=THREAD_MAX_BYTES,
        backupCount=THREAD_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,  # create file lazily on first emit
    )
    handler.setFormatter(LOG_FORMATTER)
    handler.addFilter(ContextFilter())
    _NAMED_HANDLERS[log_name] = handler
    return handler


def configure_logging(
    logger_name: Optional[str] = None,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    named_log: Optional[str] = None,
) -> logging.LoggerAdapter:
    """
    Configure root logging once and return a logger adapter with optional context.
    When thread_id is provided, logs are also written to logs/log_<thread_id>.txt.
    When named_log is provided, logs are also written to logs/<named_log>.log.
    """
    _ensure_base_logging()

    logger = logging.getLogger(logger_name)
    if thread_id:
        handler = _get_thread_handler(thread_id)
        if handler not in logger.handlers:
            logger.addHandler(handler)
    if named_log:
        handler = _get_named_handler(named_log)
        if handler not in logger.handlers:
            logger.addHandler(handler)

    return logging.LoggerAdapter(logger, {"thread_id": thread_id or "-", "user_id": user_id or "-"})


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