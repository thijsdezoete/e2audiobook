from fastapi import APIRouter

from narrator.api.routes_books import router as books_router
from narrator.api.routes_jobs import router as jobs_router
from narrator.api.routes_queue import router as queue_router
from narrator.api.routes_settings import router as settings_router
from narrator.api.routes_system import router as system_router
from narrator.api.routes_voices import router as voices_router

api_router = APIRouter()
api_router.include_router(system_router, tags=["system"])
api_router.include_router(books_router, prefix="/books", tags=["books"])
api_router.include_router(queue_router, prefix="/queue", tags=["queue"])
api_router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
api_router.include_router(voices_router, prefix="/voices", tags=["voices"])
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])
