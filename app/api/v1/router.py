from fastapi import APIRouter

from app.api.v1 import auth, oauth

api_router = APIRouter()
api_router.include_router(auth.router)
# Nested under /auth so every sign-in route shares one prefix.
api_router.include_router(oauth.router, prefix="/auth")
