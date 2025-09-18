from fastapi import APIRouter, Request, status, HTTPException, Query, Header
from src.auth import AuthRequired

router = APIRouter(dependencies=[AuthRequired])

@router.get("/availablechatbots")
async def availablechatbots():
    # Phase 1: surface only
    return {"ok": True, "data": [], "note": "stub - Phase 2 will fill"}

@router.get("/getthread")
async def getthread(
    thread_id: str | None = Query(default=None, description="Thread identifier (required)")
):
    # Phase 1: enforce presence to mirror Rust's required param semantics
    if not thread_id or not thread_id.strip():
        # TODO(parity): adjust exact error text to match Rust if needed
        raise HTTPException(status_code=422, detail="Missing required parameter: thread_id")
    return {"ok": True, "thread_id": thread_id, "note": "stub - Phase 2 will fill"}

@router.get("/getuserthreads")
async def getuserthreads(request: Request):
    # Username injected by auth dependency
    user = getattr(request.state, "username", None)
    return {"ok": True, "user": user, "threads": [], "note": "stub - Phase 2 will fill"}

@router.get("/streamresponse")
async def streamresponse(
    request: Request,
    thread_id: str | None = Query(default=None, description="Existing thread to continue (optional)"),
    user_input: str | None = Query(default=None, alias="user_input", description="User message (optional)"),
    x_freva_configpath: str | None = Header(default=None, alias="X-Freva-ConfigPath", description="Config path header (optional)"),
):
    # Phase 1: define interface only; no streaming yet
    user = getattr(request.state, "username", None)
    return {
        "ok": True,
        "mode": "non-streaming-stub",
        "user": user,
        "thread_id": thread_id,
        "user_input": user_input,
        "config_path": x_freva_configpath,
        "note": "stub - Phase 2/3 will fill",
    }

@router.get("/stop")
async def stop_get(
    thread_id: str | None = Query(default=None, description="Thread to stop (optional)")
):
    # Phase 1: accept signal; actual stop logic later
    return {"ok": True, "stopped": True, "thread_id": thread_id, "note": "stub - Phase 2 will fill"}

@router.post("/stop", status_code=status.HTTP_200_OK)
async def stop_post(
    thread_id: str | None = Query(default=None, description="Thread to stop (optional)")
):
    return {"ok": True, "stopped": True, "thread_id": thread_id, "note": "stub - Phase 2 will fill"}

@router.get("/heartbeat")
async def heartbeat():
    # Simple liveness probe
    return {"ok": True}
