from fastapi import APIRouter

from .availablechatbots import router as availablechatbots_router
from .getthread import router as getthread_router
from .getuserthreads import router as getuserthreads_router
from .deletethread import router as deletethread_router
from .setthreadtopic import router as setthreadtopic_router
from .searchthreads import router as searchthreads_router
from .streamresponse import router as streamresponse_router
from .stop import router as stop_router

router = APIRouter()

# Mount individual endpoint routers
router.include_router(availablechatbots_router)
router.include_router(getthread_router)
router.include_router(getuserthreads_router)
router.include_router(deletethread_router)
router.include_router(setthreadtopic_router)
router.include_router(searchthreads_router)
router.include_router(streamresponse_router)
router.include_router(stop_router)
