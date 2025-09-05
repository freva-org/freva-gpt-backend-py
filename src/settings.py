from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env file


@dataclass(frozen=True)
class Settings:
    HOST: str = os.getenv("HOST", "0.0.0.0")
    BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8502"))
    AUTH_KEY: str = os.getenv("AUTH_KEY", "")       
    ALLOW_GUESTS: bool = os.getenv("ALLOW_GUESTS", "false").lower() in {"1", "true", "yes"}
    LITE_LLM_ADDRESS: str = os.getenv("LITE_LLM_ADDRESS", "http://litellm:4000")
    VERSION: str = os.getenv("VERSION", "0.1.0")      


# Simple singleton-style accessor (mirrors Rust OnceCell)
_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS