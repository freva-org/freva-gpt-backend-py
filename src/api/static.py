from fastapi import APIRouter
from src.core.settings import Settings

router = APIRouter()

@router.get("/ping")
def ping():
    #TODO: Placeholder, should mirror Rust's return
    return {"status": "ok"}

@router.get("/docs")
def docs():
    #TODO: Placeholder
    return {"FrevaGPT Backend API docs"}


@router.get("/help")
def help():
    #TODO: Placeholder
    return {"Help"}