from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


@router.get("/getthread")
async def getthread(
    thread_id: str | None = Query(default=None, description="Thread identifier (required)")
):
    # Phase 1: enforce presence to mirror Rust's required param semantics
    if not thread_id or not thread_id.strip():
        # TODO(parity): adjust exact error text to match Rust if needed
        raise HTTPException(status_code=422, detail="Missing required parameter: thread_id")
    return {"ok": True, "thread_id": thread_id, "note": "stub - Phase 2 will fill"}


# TODO: stubs