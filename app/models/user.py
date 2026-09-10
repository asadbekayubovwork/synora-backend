from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    # Always stored lowercase — `normalize_email` is the only way in. Null for
    # an account created through a provider that gives us no address: Telegram
    # hands back a numeric id and nothing else.
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)

    # Null for an account that has only ever signed in through a provider.
    # Clients branch on `has_password`, not on this.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Both filled from the provider profile when we learn them, and never
    # overwritten once set — the user's own edit outranks the provider's copy.
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # False until the registration OTP is verified; such a row is a pending
    # signup, not an account, and cannot log in.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Grants the `/admin` routes, which move real money — a manual credit, a
    # refund, publishing a price. A column rather than an `ADMIN_EMAILS`
    # allowlist for three reasons: a Telegram-only account has no email and so
    # could never be an admin; an email-based allowlist would turn any future
    # "change my email" endpoint into privilege escalation; and every admin
    # action needs attribution to a real user id, which the ledger records.
    # Set it with `devtools/set_superuser.py` until there is a UI for it.
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def has_password(self) -> bool:
        """False for a provider-only account, which cannot use `/auth/login`."""
        return bool(self.password_hash)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} verified={self.is_verified}>"


def normalize_email(email: str) -> str:
    return email.strip().lower()
