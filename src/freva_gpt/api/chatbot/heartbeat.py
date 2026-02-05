from fastapi import APIRouter

from freva_gpt.services.service_factory import AuthRequired

router = APIRouter()


@router.get("/heartbeat", dependencies=[AuthRequired])
async def heartbeat():
    # Simple liveness probe
    return {"ok": True}
