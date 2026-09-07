from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OAuthProviderName(str, enum.Enum):
    GOOGLE = "google"
    GITHUB = "github"
    TELEGRAM = "telegram"


class OAuthAccount(Base):
    """A provider identity that signs in as one of our users.

    The provider's own account id is the join key, never the email: a user can
    change their Google address, and a GitHub handle can be released and
    claimed by somebody else, but the numeric id is stable and unrecycled.

    At most one row per (provider, account id) — the unique constraint is what
    stops the same Google account from opening two Synora accounts — and the
    service layer additionally keeps it to one account per provider per user.
    """

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_oauth_provider_account"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    provider: Mapped[OAuthProviderName] = mapped_column(
        Enum(OAuthProviderName, native_enum=False, length=32),
        nullable=False,
    )
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Kept for display on the "linked accounts" screen, and refreshed on every
    # sign-in. The account's own email lives on `User`.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OAuthAccount {self.provider.value}:{self.provider_account_id}>"
