from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OtpPurpose(str, enum.Enum):
    REGISTER = "register"
    RESET_PASSWORD = "reset_password"


class OtpCode(Base):
    """A one-time code issued to an email address.

    Keyed by email rather than user id so a code can be issued before the
    account exists. At most one live code per (email, purpose): issuing a new
    one deletes whatever came before.
    """

    __tablename__ = "otp_codes"

    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    purpose: Mapped[OtpPurpose] = mapped_column(
        Enum(OtpPurpose, native_enum=False, length=32),
        default=OtpPurpose.REGISTER,
        nullable=False,
    )

    # HMAC-SHA256 of the code — the code itself is never stored.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OtpCode {self.email} {self.purpose.value} attempts={self.attempts}>"
