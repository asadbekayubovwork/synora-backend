from fastapi import APIRouter

from app.api.v1 import admin, auth, oauth, tts, usage, wallet

api_router = APIRouter()
api_router.include_router(auth.router)
# Nested under /auth so every sign-in route shares one prefix.
api_router.include_router(oauth.router, prefix="/auth")
api_router.include_router(wallet.router)
api_router.include_router(tts.router)
# Its own prefix rather than a `/wallet` sub-route: consumption is not money,
# and the day STT and chat report here nobody should have to explain why usage
# lives under the wallet.
api_router.include_router(usage.router)
api_router.include_router(admin.router)
