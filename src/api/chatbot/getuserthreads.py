from fastapi import APIRouter, Request, HTTPException
from starlette.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_422_UNPROCESSABLE_ENTITY, HTTP_500_INTERNAL_SERVER_ERROR

from src.services.storage import router as storage_router
from src.services.storage import mongodb_storage 
from src.core.auth import AuthRequired

router = APIRouter()

# TODO: check parity with Rust - check compatibility with frontend
@router.get("/getuserthreads", dependencies=[AuthRequired])
async def get_user_threads(request: Request):
    """
    Returns the latest 10 threads of a user.
    Rust parity:
    - Requires header x-freva-vault-url
    - user_id can come from auth, or fallback query param if fallback enabled
    - Always queries Mongo (not Disk)
    """
    headers = request.headers
    vault_url = headers.get("x-freva-vault-url")
    if not vault_url:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Vault URL not found. Please provide a non-empty vault URL in the headers.",
        )

    # Username is provided by AuthRequired (your auth layer sets request.state.username)
    username = getattr(request.state, "username", None) or getattr(request.state, "user", None)
    if not username:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing authenticated username.",
        )

    # Connect to database
    try:
        database = await mongodb_storage.get_database(vault_url)
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to connect to the database: {e}",
        )

    # Fetch threads
    threads = await storage_router.read_thread(username, database)

    return JSONResponse(content=threads)
