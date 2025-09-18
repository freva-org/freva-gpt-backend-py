from fastapi import APIRouter
from src.auth import AuthRequired

from .availablechatbots import router as availablechatbots_router
from .getthread import router as getthread_router
from .getuserthreads import router as getuserthreads_router
from .streamresponse import router as streamresponse_router
from .stop import router as stop_router
from .heartbeat import router as heartbeat_router

# Aggregate router with shared auth dependency (parity with the single-file version)
router = APIRouter(dependencies=[AuthRequired])

# Mount individual endpoint routers
router.include_router(availablechatbots_router)
router.include_router(getthread_router)
router.include_router(getuserthreads_router)
router.include_router(streamresponse_router)
router.include_router(stop_router)
router.include_router(heartbeat_router)