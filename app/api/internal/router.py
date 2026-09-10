from fastapi import APIRouter

from app.api.internal import health

internal_router = APIRouter()
internal_router.include_router(health.router)
