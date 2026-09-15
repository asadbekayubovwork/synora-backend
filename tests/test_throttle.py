"""The login throttle, and the two ways it is allowed to let a caller through.

The expense being rationed is bcrypt: `/auth/login` runs it even for an address
with no account, so that the answer cannot be used to discover which emails are
registered. That property is worth keeping and worth not paying for
unboundedly. These tests drive the limiter directly through a stub cache rather
than through a live Redis, for the reason `test_cache.py` gives — the
interesting halves are the boundary and the outage, and both are easier to
arrange than to wait for.
"""

from __future__ import annotations

import pytest

from app.core import throttle
from app.core.cache import NullCache
from app.core.exceptions import TooManyRequestsError


class CountingCache(NullCache):
    """A cache that counts, and can be told to go away mid-test."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    async def increment(self, key: str, ttl_seconds: int) -> int:
        if self.down:
            # What `RedisCache.increment` returns once it has degraded.
            return 0
        self.counts[key] = self.counts.get(key, 0) + 1
        self.ttls.setdefault(key, ttl_seconds)
        return self.counts[key]


@pytest.fixture
def cache(monkeypatch) -> CountingCache:
    stub = CountingCache()
    monkeypatch.setattr(throttle, "get_cache", lambda: stub)
    return stub


async def _attempt(**over) -> None:
    await throttle.enforce(
        **{
            "subject": "ali@example.com",
            "action": "login",
            "limit": 3,
            "window_seconds": 60,
            "message": "too many",
            "code": "login_throttled",
            **over,
        }
    )


async def test_the_limit_is_the_last_attempt_allowed_not_the_first_refused(cache):
    """Off-by-one is the whole risk in a limiter, so it is pinned."""
    for _ in range(3):
        await _attempt()

    with pytest.raises(TooManyRequestsError) as refused:
        await _attempt()

    assert refused.value.code == "login_throttled"
    assert refused.value.status_code == 429


async def test_a_refusal_says_when_to_come_back(cache):
    """`Retry-After` names the real end of the window.

    A client that honours it returns when the budget has actually reset. A
    made-up number would either send them back too early — into another 429 —
    or hold them out longer than the limit does.
    """
    for _ in range(4):
        try:
            await _attempt()
        except TooManyRequestsError as refused:
            assert 1 <= refused.retry_after <= 60
            assert refused.headers["Retry-After"] == str(refused.retry_after)
            return
    pytest.fail("never refused")


async def test_two_subjects_do_not_share_a_budget(cache):
    """One account running out must not lock out the next caller."""
    for _ in range(3):
        await _attempt(subject="ali@example.com")

    await _attempt(subject="vali@example.com")  # must not raise


async def test_two_actions_do_not_share_a_budget(cache):
    """The per-email and per-IP windows are counted separately."""
    for _ in range(3):
        await _attempt(action="login")

    await _attempt(action="login_ip")  # must not raise


async def test_the_window_is_what_the_counter_expires_on(cache):
    """A window that is re-set on every hit never rolls, and the limit becomes
    permanent for anyone under sustained load."""
    await _attempt(window_seconds=90)
    assert set(cache.ttls.values()) == {90}


async def test_without_a_cache_the_throttle_is_off_rather_than_broken(cache):
    """The documented policy for guards, stated in `cache.py`.

    A protection that takes sign-in down when its accelerator blinks has chosen
    the worse outage — so a degraded Redis lets callers through. It also means
    the throttle is a brake on abuse and never the only thing between an
    attacker and an account; that is what the password is for.
    """
    cache.down = True

    for _ in range(50):
        await _attempt()  # must never raise


async def test_the_real_null_cache_agrees(monkeypatch):
    """Not just the stub: the shipped no-Redis path behaves the same way."""
    monkeypatch.setattr(throttle, "get_cache", NullCache)

    for _ in range(50):
        await _attempt()
