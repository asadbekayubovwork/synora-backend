"""The cursor codec — a repo-wide convention, so it gets its own tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.exceptions import BadRequestError
from app.schemas.common import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    clamp_limit,
    decode_cursor,
    ensure_utc,
)


def test_a_cursor_round_trips():
    cursor = Cursor(created_at=datetime(2026, 9, 7, 12, 34, 56, tzinfo=UTC), row_id=uuid.uuid4())

    assert Cursor.decode(cursor.encode()) == cursor


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time():
    """The bug this exists to prevent.

    SQLite returns timestamps without an offset. `astimezone(UTC)` on a naive
    value assumes it is *local*, so on a UTC+5 box a cursor built from a stored
    row was stamped five hours early — and the next page silently filtered out
    every remaining row. The stored value is already UTC; it just has not said
    so.
    """
    naive = datetime(2026, 9, 7, 18, 45, 6, 698130)
    row_id = uuid.uuid4()

    decoded = Cursor.decode(Cursor(created_at=naive, row_id=row_id).encode())

    assert decoded.created_at == naive.replace(tzinfo=UTC)
    assert decoded.created_at.hour == 18, "not shifted by the local offset"


def test_a_cursor_carries_no_padding_so_it_is_url_clean():
    token = Cursor(created_at=datetime.now(UTC), row_id=uuid.uuid4()).encode()

    assert "=" not in token
    assert "/" not in token and "+" not in token


@pytest.mark.parametrize("bad", ["", "not-a-cursor", "!!!!", "YWJj", "MjAyNnxub3QtYS11dWlk"])
def test_a_malformed_cursor_is_a_clean_four_hundred(bad):
    with pytest.raises(BadRequestError) as excinfo:
        Cursor.decode(bad)

    assert excinfo.value.code == "cursor_invalid"


def test_no_cursor_means_start_at_the_beginning():
    assert decode_cursor(None) is None


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, DEFAULT_PAGE_SIZE), (1, 1), (50, 50), (10_000, MAX_PAGE_SIZE), (0, 1), (-5, 1)],
)
def test_an_absurd_limit_is_capped_rather_than_refused(requested, expected):
    """A 422 teaches the client nothing it cannot work out from the response;
    capping protects the database either way."""
    assert clamp_limit(requested) == expected


def test_ensure_utc_leaves_an_aware_timestamp_alone():
    aware = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

    assert ensure_utc(aware) is aware
    assert ensure_utc(None) is None
