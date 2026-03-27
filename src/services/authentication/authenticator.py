from __future__ import annotations
from dataclasses import dataclass

from abc import ABC, abstractmethod
from typing import Optional

from fastapi import Request

from src.core.settings import Settings

# ──────────────────── Base Authenticator Class ──────────────────────────────


@dataclass
class Authenticator(ABC):
    """
    Per-request authenticator.

    - Holds request + settings
    - After `await run()`, attributes like `username`, `vault_url`, `rest_url`
      are populated (or HTTPException is raised).
    """

    request: Request
    settings: Settings
    username: str
    vault_url: Optional[str]
    rest_url: str
    access_token: str

    @abstractmethod
    async def build(request: Request) -> Authenticator:
        """
        Builds the Authenticator instance for the given request.
        Raises HTTPException if authentication fails.
        """
