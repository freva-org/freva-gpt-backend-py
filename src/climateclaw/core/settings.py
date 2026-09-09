from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar

from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env file


@dataclass(frozen=True)
class Settings:
    VERSION: str = os.getenv("CLIMATECLAW_VERSION", "0.1.0")
    HOST: str = os.getenv("CLIMATECLAW_HOST", "0.0.0.0")
    BACKEND_PORT: int = int(os.getenv("CLIMATECLAW_BACKEND_PORT", "8502"))
    LITE_LLM_ADDRESS: str = os.getenv(
        "CLIMATECLAW_LITE_LLM_ADDRESS", "http://litellm:4000"
    )
    AVAILABLE_MCP_SERVERS: ClassVar[list[str]] = [
        s for s in os.getenv("CLIMATECLAW_AVAILABLE_MCP_SERVERS", "").split(",")
    ]
    MONGODB_DATABASE_NAME: str = os.getenv(
        "CLIMATECLAW_MONGODB_DATABASE_NAME", "chatbot"
    )
    MONGODB_COLLECTION_NAME: str = os.getenv(
        "CLIMATECLAW_MONGODB_COLLECTION_NAME", "threads"
    )
    MONGODB_COLLECTION_NAME_EMB: str = os.getenv(
        "CLIMATECLAW_MONGODB_COLLECTION_NAME_EMB", "embeddings"
    )
    CLEAR_MONGODB_EMBEDDINGS: bool = os.getenv(
        "CLIMATECLAW_CLEAR_MONGODB_EMBEDDINGS", ""
    ).lower() in {"1", "true", "yes"}
    MCP_REQUEST_TIMEOUT_SEC: int = int(
        os.getenv("CLIMATECLAW_MCP_REQUEST_TIMEOUT_SEC", "600")
    )
    DEV: bool = os.getenv("CLIMATECLAW_DEV", "").lower() in {"1", "true", "yes"}
    SYSLOG_TARGET = os.getenv("CLIMATECLAW_SYSLOG_TARGET")


# Simple singleton-style accessor
_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def get_server_url_dict(server_list: list):
    url_dict: dict[str, str] = {}
    for s in server_list:
        s_url = os.getenv(f"CLIMATECLAW_{s.upper().replace('-', '_')}_URL", "")
        if s_url:
            url_dict.update({s: s_url})
        else:
            raise ValueError(f"Please set url address for MCP server {s}!")
    return url_dict
