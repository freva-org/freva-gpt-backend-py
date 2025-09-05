from fastapi import APIRouter, status

router = APIRouter()

@router.get("/availablechatbots")
def availablechatbots():
    return {"ok": True, "data": [], "note": "stub"}

@router.get("/getthread")
def getthread(thread_id: str | None = None):
    if not thread_id:
        return {"ok": False, "error": "missing thread_id (stub)"}
    return {"ok": True, "thread_id": thread_id, "note": "stub"}

@router.get("/getuserthreads")
def getuserthreads(user: str | None = None):
    return {"ok": True, "user": user, "threads": [], "note": "stub"}

@router.get("/streamresponse")
def streamresponse():
    # Non-streaming in early phases; final behavior comes later.
    return {"ok": True, "message": "stub "}

@router.get("/stop")
def stop_get():
    return {"ok": True, "stopped": True, "note": "stub"}

@router.post("/stop", status_code=status.HTTP_200_OK)
def stop_post():
    return {"ok": True, "stopped": True, "note": "stub"}

#TODO: All place folders