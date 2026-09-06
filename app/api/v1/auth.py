from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep, get_user_from_refresh_token
from app.core.config import settings
from app.schemas.auth import (
    ErrorResponse,
    LoginRequest,
    OtpSentResponse,
    RefreshTokenRequest,
    RegisterRequest,
    ResendOtpRequest,
    TokenResponse,
    UserResponse,
    VerifyOtpRequest,
)
from app.services import auth_service
from app.services.otp_service import IssuedOtp

router = APIRouter(prefix="/auth", tags=["Auth"])

ERRORS: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    429: {"model": ErrorResponse, "description": "Too many requests"},
}


def _otp_sent(email: str, issued: IssuedOtp, message: str) -> OtpSentResponse:
    return OtpSentResponse(
        message=message,
        email=email,
        expires_in=issued.expires_in,
        resend_available_in=issued.resend_available_in,
        # Never populated outside development — see `Settings.expose_otp`.
        dev_code=issued.code if settings.expose_otp else None,
    )


@router.post(
    "/register",
    response_model=OtpSentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
    summary="Register — step 1 of 2",
    description=(
        "Takes an email and password and mails a 6-digit code to the address.\n\n"
        "**No account exists yet at this point.** The row is stored unverified and "
        "cannot sign in until `POST /auth/verify-otp` succeeds. Calling this again "
        "for an unfinished signup replaces the password and issues a new code; "
        "calling it for a verified account returns `409`.\n\n"
        "In development the response carries `dev_code` so you can test without a mailbox."
    ),
)
async def register(payload: RegisterRequest, session: SessionDep) -> OtpSentResponse:
    issued = await auth_service.register(session, payload.email, payload.password)
    return _otp_sent(payload.email, issued, "A verification code has been sent to your email.")


@router.post(
    "/verify-otp",
    response_model=TokenResponse,
    responses=ERRORS,
    summary="Register — step 2 of 2 (verify the emailed code)",
    description=(
        "Confirms the code from step 1, activates the account and signs the user in, "
        "so the client can go straight to the app.\n\n"
        f"Codes expire after {settings.otp_ttl_minutes} minutes and are burned on use. "
        f"After {settings.otp_max_attempts} wrong guesses the code is discarded and a "
        "new one must be requested."
    ),
)
async def verify_otp(payload: VerifyOtpRequest, session: SessionDep) -> TokenResponse:
    tokens = await auth_service.verify_registration_otp(session, payload.email, payload.code)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(tokens.user),
    )


@router.post(
    "/resend-otp",
    response_model=OtpSentResponse,
    responses=ERRORS,
    summary="Send a new verification code",
    description=(
        "Issues a fresh code for an unfinished signup and invalidates the previous one.\n\n"
        f"Rate limited to one code every {settings.otp_resend_cooldown_seconds} seconds; "
        "over that it returns `429` with a `Retry-After` header."
    ),
)
async def resend_otp(payload: ResendOtpRequest, session: SessionDep) -> OtpSentResponse:
    issued = await auth_service.resend_registration_otp(session, payload.email)
    return _otp_sent(payload.email, issued, "A new verification code has been sent.")


@router.post(
    "/login",
    response_model=TokenResponse,
    responses=ERRORS,
    summary="Log in with email and password",
    description=(
        "Returns an access/refresh token pair for a verified account.\n\n"
        "Wrong password and unknown email answer identically (`401`), so this endpoint "
        "cannot be used to discover which emails are registered. An account that never "
        "finished verification gets `403` with code `email_not_verified` — send the user "
        "back to the code step via `POST /auth/resend-otp`."
    ),
)
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    tokens = await auth_service.login(session, payload.email, payload.password)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(tokens.user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses=ERRORS,
    summary="Exchange a refresh token for a new token pair",
)
async def refresh(payload: RefreshTokenRequest, session: SessionDep) -> TokenResponse:
    user = await get_user_from_refresh_token(session, payload.refresh_token)
    tokens = await auth_service.refresh_tokens(session, user)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(tokens.user),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    responses=ERRORS,
    summary="The signed-in user",
    description="Requires `Authorization: Bearer <access_token>`.",
)
async def me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)
