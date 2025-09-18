from fastapi import APIRouter

router = APIRouter()

@router.get("/availablechatbots")
async def availablechatbots():
    # Phase 1: surface only
    return {"ok": True, "data": [], "note": "stub - Phase 2 will fill"}