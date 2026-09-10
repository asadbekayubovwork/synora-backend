"""Redis, and the Postgres-shaped hole where Redis isn't.

Redis is an accelerator here, never a source of truth. It gates — "may this
session keep running?", "is this nonce fresh?", "how many calls does this user
have open?" — while Postgres decides what money moved. A Redis flush is
therefore allowed to cost latency and log noise, and is never allowed to cost
correctness.

That is only credible if the fallback is exercised, so `REDIS_URL` is empty in
the test suite and every call site here has a `NullCache` path that the whole
suite runs through. A fallback nobody runs is a fallback that does not work.

The two policies worth stating out loud, because they point in opposite
directions and both are deliberate:

- **A missing key is not a zero.** If the live counters for an active session
  have gone, the caller must rehydrate them from Postgres. Treating absence as
  "nothing used yet" would hand out free usage after every restart.
- **A failed *guard* fails open.** The nonce replay check and the rate limits
  are protections, not ledgers; dropping a money-bearing report because Redis
  blinked is the worse outcome, and replay is already bounded by the signature
  timestamp window and the per-report idempotency key.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from app.core.config import settings

logger = logging.getLogger("synora.cache")


@runtime_checkable
class Cache(Protocol):
    """The narrow surface the rest of the app is allowed to depend on."""

    @property
    def is_available(self) -> bool: ...

    async def claim_once(self, key: str, ttl_seconds: int) -> bool:
        """True if this caller is the first to claim `key` within its TTL.

        The primitive behind single-use nonces and single-use tokens.
        """

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def increment(self, key: str, ttl_seconds: int) -> int:
        """Bump a counter, setting its expiry on first use. Returns the total."""

    async def close(self) -> None: ...


class NullCache:
    """No Redis. Every method is a no-op that admits it is a no-op.

    `claim_once` returns True — fail open. It is a replay *guard*, and the
    signature timestamp window plus the idempotency keys are the actual
    correctness mechanisms; refusing real traffic because there is nowhere to
    remember a nonce would be trading a small risk for a certain outage.
    """

    @property
    def is_available(self) -> bool:
        return False

    async def claim_once(self, key: str, ttl_seconds: int) -> bool:  # noqa: ARG002
        return True

    async def get(self, key: str) -> str | None:  # noqa: ARG002
        return None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> None:
        return None

    async def increment(self, key: str, ttl_seconds: int) -> int:  # noqa: ARG002
        # Zero, not one: a caller comparing this against a limit must not be
        # able to trip it. Rate limiting without Redis is off, not broken.
        return 0

    async def close(self) -> None:
        return None


class RedisCache:
    """Redis, with every failure downgraded to the `NullCache` answer.

    A Redis outage should read like Redis being absent, which is a state the
    app is already designed for — not like a new failure mode nobody has
    thought about.
    """

    def __init__(self, url: str, prefix: str) -> None:
        # Imported here so `redis` stays an optional runtime dependency in
        # practice as well as in principle.
        from redis.asyncio import Redis

        self._client = Redis.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._degraded_since: float | None = None

    @property
    def is_available(self) -> bool:
        return self._degraded_since is None

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def _degrade(self, operation: str, error: Exception) -> None:
        # Logged once per outage rather than once per request: a Redis that is
        # down is down for thousands of requests, and drowning the log is how
        # the *next* problem goes unnoticed.
        if self._degraded_since is None:
            self._degraded_since = time.monotonic()
            logger.error("Redis unavailable during %s: %s", operation, error)

    def _recover(self) -> None:
        if self._degraded_since is not None:
            logger.info("Redis is answering again")
            self._degraded_since = None

    async def claim_once(self, key: str, ttl_seconds: int) -> bool:
        try:
            claimed = await self._client.set(self._key(key), "1", nx=True, ex=ttl_seconds)
            self._recover()
            return bool(claimed)
        except Exception as error:  # noqa: BLE001 - any failure is "no Redis"
            self._degrade("claim_once", error)
            return True

    async def get(self, key: str) -> str | None:
        try:
            value = await self._client.get(self._key(key))
            self._recover()
            return value
        except Exception as error:  # noqa: BLE001
            self._degrade("get", error)
            return None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        try:
            await self._client.set(self._key(key), value, ex=ttl_seconds)
            self._recover()
        except Exception as error:  # noqa: BLE001
            self._degrade("set", error)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._client.delete(*(self._key(key) for key in keys))
            self._recover()
        except Exception as error:  # noqa: BLE001
            self._degrade("delete", error)

    async def increment(self, key: str, ttl_seconds: int) -> int:
        try:
            namespaced = self._key(key)
            total = await self._client.incr(namespaced)
            if total == 1:
                # Only the first caller sets the window; re-setting it on every
                # hit would turn a fixed window into a sliding one that never
                # expires under sustained load.
                await self._client.expire(namespaced, ttl_seconds)
            self._recover()
            return int(total)
        except Exception as error:  # noqa: BLE001
            self._degrade("increment", error)
            return 0

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception as error:  # noqa: BLE001
            logger.warning("Closing Redis raised: %s", error)


_cache: Cache | None = None


def get_cache() -> Cache:
    """The process-wide cache, built on first use.

    Lazily, not in the lifespan, because `tests/conftest.py` runs the app
    through `ASGITransport` where the lifespan never fires — a
    lifespan-constructed client would be `None` in every test. Same reasoning
    as the outbound HTTP clients.
    """
    global _cache
    if _cache is None:
        if settings.has_redis:
            _cache = RedisCache(settings.redis_url, settings.redis_prefix)
            logger.info("Redis cache enabled (prefix=%s)", settings.redis_prefix)
        else:
            _cache = NullCache()
            logger.info("No REDIS_URL: running on Postgres fallbacks only")
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None


# --- key names, in one place ----------------------------------------------
#
# Spelled out here rather than formatted at each call site, so a key's shape
# and its TTL are decided together and `redis-cli --scan` is readable.


def nonce_key(key_id: str, nonce: str) -> str:
    return f"nonce:{key_id}:{nonce}"


def session_jti_key(jti: str) -> str:
    return f"jti:{jti}"


def session_state_key(session_id: str) -> str:
    return f"sess:{session_id}"


def user_sessions_key(user_id: str) -> str:
    return f"user:{user_id}:sessions"


def rate_limit_key(subject: str, action: str, window_start: int) -> str:
    return f"rl:{subject}:{action}:{window_start}"


def job_lock_key(name: str) -> str:
    return f"job:{name}:lock"
