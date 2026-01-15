from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from freva_gpt import __version__

from .api import chatbot, static
from .core.logging_setup import configure_logging
from .core.runtime_checks import run_startup_checks
from .core.settings import get_settings
from .services.streaming.active_conversations import cleanup_idle

settings = get_settings()

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app (skeleton)
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup (was @app.on_event("startup"))
    configure_logging()
    run_startup_checks(get_settings())

    async def periodic_cleanup_task():
        while True:
            try:
                await asyncio.sleep(60 * 60)  # check every hour
                # Storage is not needed here, conversation must have been saved when it was last used
                evicted = await cleanup_idle(max_idle=timedelta(days=1))
                if evicted:
                    print("Evicted idle > 1 day:", evicted)
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Don’t crash the task; log and continue
                print("Daily cleanup failed:", e)

    # Launch background task
    app.state.periodic_cleanup = asyncio.create_task(periodic_cleanup_task())

    try:
        yield
    finally:
        # Shutdown (was @app.on_event("shutdown"))
        app.state.periodic_cleanup.cancel()


app = FastAPI(
    title="FrevaGPT Backend (Python)",
    version=__version__,
    docs_url="/docs",  # exposing FasAPI docs
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# CORS – mirror the permissive defaults (might need to adjust later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
