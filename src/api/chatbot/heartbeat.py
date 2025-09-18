from __future__ import annotations

from . import router

@router.get("/heartbeat")
async def heartbeat():
    # Simple liveness probe
    return {"ok": True}