from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
def ping() -> Dict[str, str]:
    # TODO: Placeholder, should mirror Rust's return
    return {"status": "ok"}


@router.get("/docs")
def docs() -> Any:
    # TODO: Placeholder
    return {"FrevaGPT Backend API docs"}


@router.get("/help")
def help() -> Any:
    # TODO: Placeholder
    return {"Help"}
