import logging
import os
import socket
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler, SysLogHandler
from pathlib import Path

from climateclaw.core.settings import get_settings

_SILENCED = False
_CONFIGURED = False

settings = get_settings()
ENABLE_FILE_LOGGING = os.getenv("CLIMATECLAW_FILE_LOGGING", "1") == "1"

SERVICE_NAME = os.getenv("HOSTNAME") or "app"

LOG_DIR = Path(__file__).resolve().parents[3] / "logs"
MAIN_LOG = LOG_DIR / f"{SERVICE_NAME}.log"
MAIN_MAX_BYTES = 5_000_000
MAIN_BACKUP_COUNT = 5

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    f"[service={SERVICE_NAME}]"
    "[request=%(request_id)s thread=%(thread_id)s user=%(user_id)s] %(message)s"
)
LOG_FORMATTER = logging.Formatter(LOG_FORMAT)

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "request_id_context", default=None
)


def get_request_id() -> str:
    return REQUEST_ID_CONTEXT.get() or "-"


def set_request_id(request_id: str | None):
    return REQUEST_ID_CONTEXT.set(request_id or None)


def reset_request_id(token) -> None:
    REQUEST_ID_CONTEXT.reset(token)


def _parse_syslog_target(
    target: str,
) -> tuple[tuple[str, int], socket.SocketKind] | None:
    """
    Parse HAProxy-style syslog targets like tcp@host:1514.
    Returns the address and socket type expected by SysLogHandler.
    """
    protocol, separator, address = target.partition("@")
    if separator != "@" or protocol not in {"tcp", "udp"}:
        return None

    host, separator, port = address.rpartition(":")
    if separator != ":" or not host:
        return None

    try:
        parsed_port = int(port)
    except ValueError:
        return None

    socket_type: socket.SocketKind = (
        socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
    )
    return (host, parsed_port), socket_type


class ContextFilter(logging.Filter):
    """Ensures thread_id/user_id keys exist on log records."""

    def __init__(
        self,
        thread_id: str | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__()
        self.thread_id = thread_id or "-"
        self.user_id = user_id or "-"
        self.request_id = request_id or "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_id = getattr(record, "thread_id", self.thread_id) or "-"
        record.user_id = getattr(record, "user_id", self.user_id) or "-"
        record_request_id = getattr(record, "request_id", None)
        record.request_id = (
            get_request_id() if record_request_id in (None, "-") else record_request_id
        )
        return True


def _ensure_base_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED or not ENABLE_FILE_LOGGING:
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
        delay=True,  # create file lazily on first emit
    )
    file_handler.setFormatter(LOG_FORMATTER)
    file_handler.addFilter(base_filter)
    root.addHandler(file_handler)

    if (not settings.DEV) and settings.SYSLOG_TARGET:
        parsed_target = _parse_syslog_target(settings.SYSLOG_TARGET)
        if parsed_target:
            address, socket_type = parsed_target
            try:
                syslog_handler = SysLogHandler(
                    address=address,
                    facility=SysLogHandler.LOG_LOCAL0,
                    socktype=socket_type,
                )
                syslog_handler.setFormatter(LOG_FORMATTER)
                syslog_handler.ident = f"{SERVICE_NAME}"
                syslog_handler.addFilter(base_filter)
                root.addHandler(syslog_handler)
            except OSError as e:
                root.warning("Failed to configure remote syslog logging: %s", e)
        else:
            root.warning(
                "Invalid CLIMATECLAW_SYSLOG_TARGET=%r; expected tcp@host:port or udp@host:port",
                settings.SYSLOG_TARGET,
            )

    logging.getLogger("uvicorn").setLevel(logging.WARNING)

    _CONFIGURED = True


def configure_logging(
    logger_name: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
) -> logging.LoggerAdapter:
    """
    Configure root logging once and return a logger adapter with optional context.
    When thread_id is provided, it is added as log context.
    """
    _ensure_base_logging()

    logger = logging.getLogger(logger_name)

    logging.getLogger("fakeredis").setLevel(logging.WARNING)
    logging.getLogger("docket").setLevel(logging.WARNING)
    logging.getLogger("pymongo").setLevel(logging.WARNING)

    return logging.LoggerAdapter(
        logger,
        {
            "thread_id": thread_id or "-",
            "user_id": user_id or "-",
            "request_id": request_id or get_request_id(),
        },
    )


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
