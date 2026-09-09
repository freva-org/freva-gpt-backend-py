from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from .api import chatbot, static
from .core.logging_setup import configure_logging
from .core.runtime_checks import run_startup_checks
from .core.settings import get_settings
from .services.storage.mongodb_storage import ThreadStorage
from .services.streaming.active_conversations import cleanup_idle

settings = get_settings()
logger = configure_logging(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app (skeleton)
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup (was @app.on_event("startup"))
    configure_logging()
    run_startup_checks(get_settings())
    app.state.thread_storage = await ThreadStorage.create()

    async def periodic_cleanup_task():
        while True:
            try:
                await asyncio.sleep(60)  # check every min
                # Storage is not needed here, conversation must have been saved when it was last used
                evicted = await cleanup_idle(max_idle=timedelta(minutes=30))
                if evicted:
                    logger.info(f"Evicted idle > 30 mins: {evicted}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Don’t crash the task; log and continue
                logger.warning(f"Daily cleanup failed: {e}")

    # Launch background task
    app.state.periodic_cleanup = asyncio.create_task(periodic_cleanup_task())

    try:
        yield
    finally:
        # Shutdown
        app.state.periodic_cleanup.cancel()
        await app.state.thread_storage.close()


app = FastAPI(
    title="ClimateClaw Backend",
    version=get_settings().VERSION,
    docs_url="/api/chatbot/docs",  # exposing FasAPI docs
    redoc_url="/api/chatbot/redoc",
    openapi_url="/api/chatbot/openapi.json",
    lifespan=lifespan,
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )

    security_schemes = openapi_schema.setdefault("components", {}).setdefault(
        "securitySchemes", {}
    )
    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
    }
    security_schemes["FrevaRestUrl"] = {
        "type": "apiKey",
        "in": "header",
        "name": "x-freva-rest-url",
    }
    security_schemes["FrevaThreadId"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Freva-Thread-Id",
    }

    for path, path_item in openapi_schema.get("paths", {}).items():
        if not path.startswith("/api/chatbot/"):
            continue

        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            operation.setdefault("security", []).append(
                {
                    "BearerAuth": [],
                    "FrevaRestUrl": [],
                    "FrevaThreadId": [],
                }
            )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


setattr(app, "openapi", custom_openapi)


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
