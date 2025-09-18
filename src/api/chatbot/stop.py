from fastapi import APIRouter, Query, status

router = APIRouter()


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


#TODO:stubs