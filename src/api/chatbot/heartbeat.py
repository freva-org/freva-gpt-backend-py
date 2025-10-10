from fastapi import APIRouter
from src.auth import AuthRequired


router = APIRouter()

@router.get("/heartbeat", dependencies=[AuthRequired])
async def heartbeat():
    # Simple liveness probe
    return {"ok": True}