from __future__ import annotations

from fastapi import FastAPI

from src.api import static, chatbot
from src.settings import Settings, get_settings
from src.logging_setup import configure_logging
from src.runtime_checks import run_startup_checks


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app (skeleton)
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="FrevaGPT",
    version=get_settings().VERSION
)


# ──────────────────────────────────────────────────────────────────────────────
# Route registry
# ──────────────────────────────────────────────────────────────────────────────

app.include_router(static.router, prefix="/api/chatbot", tags=["static"])
app.include_router(chatbot.router, prefix="/api/chatbot", tags=["chatbot"])


@app.on_event("startup")
def _on_startup() -> None:
    settings: Settings = get_settings()
    configure_logging()
    run_startup_checks(settings)
