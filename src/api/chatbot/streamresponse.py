from fastapi import APIRouter, Header, Query, Request

router = APIRouter()

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
