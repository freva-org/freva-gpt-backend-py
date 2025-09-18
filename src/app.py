from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import static, chatbot
from src.settings import Settings, get_settings
from src.logging_setup import configure_logging
from src.runtime_checks import run_startup_checks
from src.auth import close_http_client

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app (skeleton)
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup (was @app.on_event("startup"))
    configure_logging()
    run_startup_checks(get_settings())
    try:
        yield
    finally:
        # Shutdown (was @app.on_event("shutdown"))
        await close_http_client()


app = FastAPI(
    title="FrevaGPT Backend (Python)",
    version=get_settings().VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,  # ← eliminates on_event deprecation warnings
)

# CORS – mirror the permissive defaults Rust typically had (might need to adjust later)
# TODO: double check with Rust settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # adjust if Rust restricts origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────────────────────
# Route registry
# ──────────────────────────────────────────────────────────────────────────────

app.include_router(static.router, prefix="/api/chatbot", tags=["static"])
app.include_router(chatbot.router, prefix="/api/chatbot", tags=["chatbot"])

@app.get("/healthz")
def _healthz():
    # Simple liveness probe
    return {"status": "ok", "version": get_settings().VERSION}
