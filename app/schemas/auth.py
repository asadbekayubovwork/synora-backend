from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import settings


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --- Requests --------------------------------------------------------------


class EmailPasswordRequest(_Schema):
    email: EmailStr = Field(examples=["ali@example.com"])
    password: str = Field(
        min_length=settings.password_min_length,
        max_length=128,
        examples=["Str0ngPassw0rd"],
    )

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class RegisterRequest(EmailPasswordRequest):
    """Step 1 of registration. No account exists until the code is verified."""


class LoginRequest(_Schema):
    email: EmailStr = Field(examples=["ali@example.com"])
    password: str = Field(min_length=1, max_length=128, examples=["Str0ngPassw0rd"])

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class VerifyOtpRequest(_Schema):
    """Step 2 of registration."""

    email: EmailStr = Field(examples=["ali@example.com"])
    code: str = Field(
        min_length=settings.otp_length,
        max_length=settings.otp_length,
        pattern=r"^\d+$",
        description=f"The {settings.otp_length}-digit code sent to the address.",
        examples=["482913"],
    )

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        return value.strip()


class ResendOtpRequest(_Schema):
    email: EmailStr = Field(examples=["ali@example.com"])

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class RefreshTokenRequest(_Schema):
    refresh_token: str = Field(examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."])


# --- Responses -------------------------------------------------------------


class UserResponse(_Schema):
    id: uuid.UUID
    email: EmailStr
    is_verified: bool
    is_active: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # Timestamps are stored in UTC, but SQLite hands them back without an
        # offset — serialise them naive and every client reads them as local time.
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class TokenResponse(_Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")
    user: UserResponse


class OtpSentResponse(_Schema):
    ok: bool = True
    message: str
    email: EmailStr
    expires_in: int = Field(description="Seconds until the code expires.")
    resend_available_in: int = Field(description="Seconds before a resend is accepted.")
    dev_code: str | None = Field(
        default=None,
        description="The code itself — development only, so you can test without a mailbox.",
    )


class MessageResponse(_Schema):
    ok: bool = True
    message: str


class ErrorResponse(_Schema):
    """The shape of every non-2xx body."""

    detail: str = Field(description="Human-readable error message.")
    status_message: str = Field(
        alias="statusMessage",
        description="Same text as `detail`; the Nuxt frontend reads errors from here.",
    )
    code: str = Field(description="Stable machine-readable code, e.g. `email_already_registered`.")
