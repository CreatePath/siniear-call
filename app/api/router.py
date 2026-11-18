from fastapi import APIRouter

from app.api.health import router as health_router
from app.modules.calls.router import router as calls_router
from app.modules.calls.webhooks import router as webhooks_router
from app.modules.realtime.router import router as realtime_router

router = APIRouter(prefix="/api")
router.include_router(health_router)


router.include_router(calls_router)
router.include_router(webhooks_router)
router.include_router(realtime_router)
