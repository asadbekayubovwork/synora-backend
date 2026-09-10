"""Shapes shared across the newer endpoints, and the pagination convention.

There was no pagination anywhere in this API before the billing work: the one
list endpoint returns an unbounded array. That is fine for "the providers you
have linked" and untenable for a ledger, so this defines the convention once.

**Keyset, not offset.** A ledger and a usage log are append-only and read
newest-first, which is exactly the case where `OFFSET` breaks: rows arriving
between page one and page two shift everything down, so the reader sees a row
twice and never sees another at all. A cursor naming the last row seen cannot
drift, no matter what arrives while the reader is paging.

The cursor is opaque on purpose — base64 of `<timestamp>|<uuid>`, unsigned.
Unsigned because it carries nothing secret and grants nothing: every query it
feeds is already scoped to the caller's own rows, so the worst a forged cursor
can do is start someone's own list in an odd place. Opaque because the day the
sort key changes, no client should have to care.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.exceptions import BadRequestError

# The Nuxt client sends nothing; these are the server's own limits.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class _Schema(BaseModel):
    """Same config as `app/schemas/auth.py::_Schema`.

    Redeclared rather than imported to avoid a billing module depending on an
    auth module for a base class; the two are identical and asserted equal in
    the tests.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Reattach UTC to a timestamp SQLite handed back naive.

    Every outgoing datetime goes through this — see the `_ensure_utc`
    validators in `app/schemas/auth.py`, which exist for the same reason.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Cursor:
    """Where a page left off: the sort key of the last row it returned."""

    created_at: datetime
    row_id: uuid.UUID

    def encode(self) -> str:
        # `ensure_utc`, not `astimezone(UTC)`. SQLite hands timestamps back
        # naive, and `astimezone` on a naive value assumes *local* time — so
        # on a UTC+5 box the cursor would be stamped five hours early and the
        # next page would filter out every remaining row. The stored value is
        # already UTC; it just has not said so.
        raw = f"{ensure_utc(self.created_at).isoformat()}|{self.row_id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Cursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            timestamp, row_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
            return cls(created_at=datetime.fromisoformat(timestamp), row_id=uuid.UUID(row_id))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise BadRequestError(
                "That cursor is not one we issued. Start from the first page.",
                code="cursor_invalid",
            ) from None


def decode_cursor(value: str | None) -> Cursor | None:
    return None if value is None else Cursor.decode(value)


def clamp_limit(limit: int | None) -> int:
    """A missing or absurd limit becomes a sane one rather than an error.

    Refusing `limit=1000000` with a 422 teaches a client nothing it cannot
    work out from the response; capping it protects the database either way.
    """
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


class PageInfo(_Schema):
    """Attached to every paginated response, under `page`."""

    next_cursor: str | None = Field(
        default=None,
        description="Pass as `?cursor=` to fetch the next page. Null on the last page.",
        examples=["MjAyNi0wOS0wN1QxMjozNDo1NiswMDowMHwxYzJk..."],
    )
    has_more: bool = Field(
        default=False,
        description="Whether another page exists.",
        examples=[True],
    )
    limit: int = Field(description="How many rows this page could hold.", examples=[25])


class MessageResponse(_Schema):
    """A bare acknowledgement, matching the shape `app/schemas/auth.py` uses."""

    ok: bool = True
    message: str = Field(examples=["Done."])
