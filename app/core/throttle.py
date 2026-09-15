"""Fixed-window rate limiting, on the cache that already exists.

`cache.py` shipped `rate_limit_key` and a `NullCache.increment` that returns
zero specifically so a caller comparing against a limit cannot trip it. Both
were written for this and neither had a caller; this is the missing half.

## Fixed windows, not sliding ones

A fixed window lets a caller spend the whole budget at the end of one window
and again at the start of the next — twice the nominal rate across that seam.
That is a known and accepted property here. What this guards is expense, not
correctness: the thing being protected is ~220 ms of CPU per password check,
and a burst of 2N at a seam costs no more than raising the limit to 2N would.
A sliding window needs a sorted set per subject and a read-modify-write to
trim it, which is a real cost on every login to remove a factor-of-two.

`cache.increment` sets the expiry only when the counter is created, so the key
really does fall out at the end of its window rather than being renewed by
sustained load.

## A limit nobody can reach is not a limit

The window start is derived from the clock rather than stored, so every
process agrees on the boundary without coordinating. Two workers sharing one
Redis share one budget, which is the point — a per-process limit multiplied by
the worker count is not the number anybody wrote down.

## Failing open is the decision, not the accident

With no Redis, or with Redis degraded, `increment` returns zero and this lets
the request through. That is the policy `cache.py` states out loud for guards:
a protection that takes the service down when its accelerator blinks has
chosen the worse outage. It does mean a Redis outage removes the throttle, so
the limits here are a brake on abuse and never the only thing standing between
an attacker and an account — that is what the password is for.
"""

from __future__ import annotations

import logging
import time

from app.core.cache import get_cache, rate_limit_key
from app.core.exceptions import TooManyRequestsError

logger = logging.getLogger("synora.throttle")


async def enforce(
    *,
    subject: str,
    action: str,
    limit: int,
    window_seconds: int,
    message: str,
    code: str,
) -> None:
    """Count one attempt against `subject`, and refuse once it exceeds `limit`.

    `subject` is whatever the budget belongs to — an email, an address — and
    `action` keeps two budgets for the same subject apart. Raises
    `TooManyRequestsError` with a `Retry-After` that names the real end of the
    window, so a client that honours it comes back when the budget has
    actually reset rather than guessing.
    """
    now = int(time.time())
    window_start = now - (now % window_seconds)
    used = await get_cache().increment(
        rate_limit_key(subject, action, window_start), window_seconds
    )

    # Zero is `NullCache`, or Redis mid-degradation. Documented as off rather
    # than broken; the alternative is refusing every login because the cache
    # is unavailable.
    if used == 0:
        return

    if used > limit:
        retry_after = max(1, window_start + window_seconds - now)
        logger.info(
            "throttled action=%s used=%d limit=%d retry_after=%d",
            action,
            used,
            limit,
            retry_after,
        )
        raise TooManyRequestsError(message, code=code, retry_after=retry_after)
