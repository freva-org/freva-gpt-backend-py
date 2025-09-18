from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/getuserthreads")
async def getuserthreads(request: Request):
    # Username injected by auth dependency
    user = getattr(request.state, "username", None)
    return {"ok": True, "user": user, "threads": [], "note": "stub - Phase 2 will fill"}