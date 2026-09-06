from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False so a missing header raises our own 401 body rather than
# FastAPI's, keeping every error response the same shape.
bearer_scheme = HTTPBearer(auto_error=False, description="Paste the `access_token` from login.")
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def _user_from_token(session: AsyncSession, token: str, token_type: str) -> User:
    try:
        payload = decode_token(token, token_type)  # type: ignore[arg-type]
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Your session has expired. Please sign in again.", code="token_expired") from None
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid authentication token.", code="token_invalid") from None

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (ValueError, KeyError):
        raise UnauthorizedError("Invalid authentication token.", code="token_invalid") from None

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise UnauthorizedError("This account no longer exists.", code="user_not_found")
    if not user.is_active:
        raise ForbiddenError("This account has been disabled.", code="account_disabled")
    if not user.is_verified:
        raise ForbiddenError("Verify your email before continuing.", code="email_not_verified")

    return user


async def get_current_user(session: SessionDep, credentials: CredentialsDep) -> User:
    if credentials is None:
        raise UnauthorizedError("Authentication is required.", code="not_authenticated")
    return await _user_from_token(session, credentials.credentials, "access")


async def get_user_from_refresh_token(session: AsyncSession, token: str) -> User:
    return await _user_from_token(session, token, "refresh")


CurrentUser = Annotated[User, Depends(get_current_user)]
