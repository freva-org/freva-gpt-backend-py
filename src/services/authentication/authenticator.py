from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

from fastapi import Request

from src.core.settings import get_settings

# ──────────────────── Base Authenticator Class ──────────────────────────────

class Authenticator(ABC):
    """
    Per-request authenticator.

    - Holds request + settings
    - After `await run()`, attributes like `username`, `vault_url`, `rest_url`
      are populated (or HTTPException is raised).
    """

    def __init__(self, request: Request):
        self.request = request
        self.settings = get_settings()

        # Populated during run()
        self.username: Optional[str] = None
        self.vault_url: Optional[str] = None
        self.rest_url: Optional[str] = None
        self.access_token: Optional[str] = None

    @abstractmethod
    async def run(self) -> "Authenticator":
        """
        Perform auth. Should either:
        - raise HTTPException on failure
        - set attributes and return self on success
        """
        ...
