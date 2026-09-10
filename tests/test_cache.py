"""The cache abstraction, and the two opposite policies inside it.

There is no Redis in CI, and requiring one would mean these paths went
untested. So the Redis behaviour is driven through a stub client that
implements the handful of commands used — which is a better test than a live
server anyway: a stub can be made to fail on command, and "what happens when
Redis goes away mid-request" is the interesting half.
"""

from __future__ import annotations

import pytest

from app.core import cache as cache_module
from app.core.cache import Cache, NullCache, RedisCache, get_cache


class StubRedis:
    """Just enough Redis. `set(nx=True)` and `incr`/`expire` semantics."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False
        self.calls: list[str] = []

    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ConnectionError("stub redis is down")

    async def set(self, key, value, nx=False, ex=None):  # noqa: ANN001, FBT002
        self._check("set")
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):  # noqa: ANN001
        self._check("get")
        return self.store.get(key)

    async def delete(self, *keys):  # noqa: ANN002
        self._check("delete")
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    async def incr(self, key):  # noqa: ANN001
        self._check("incr")
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):  # noqa: ANN001
        self._check("expire")
        self.ttls[key] = ttl
        return True

    async def aclose(self):
        self._check("aclose")


@pytest.fixture
def redis_cache(monkeypatch) -> tuple[RedisCache, StubRedis]:
    stub = StubRedis()
    instance = RedisCache.__new__(RedisCache)
    instance._client = stub  # noqa: SLF001
    instance._prefix = "synora:"  # noqa: SLF001
    instance._degraded_since = None  # noqa: SLF001
    return instance, stub


# --- NullCache: the no-Redis fallback --------------------------------------


async def test_without_redis_the_replay_guard_fails_open():
    """A guard with nowhere to remember must not refuse real traffic.

    Replay is still bounded by the signature timestamp window, and every
    money-bearing endpoint is idempotent on its own key. Dropping usage reports
    because Redis is absent would be trading a small risk for a certain outage.
    """
    cache = NullCache()

    assert await cache.claim_once("n1", 600) is True
    assert await cache.claim_once("n1", 600) is True


async def test_without_redis_rate_limiting_is_off_rather_than_broken():
    """Zero can never trip a limit, so the limiter is inert instead of
    accidentally refusing everything or accidentally allowing one call."""
    assert await NullCache().increment("rl:user", 60) == 0


async def test_the_null_cache_admits_it_is_not_available():
    cache = NullCache()

    assert cache.is_available is False
    assert isinstance(cache, Cache)


# --- RedisCache: the real path ---------------------------------------------


async def test_a_nonce_can_only_be_claimed_once(redis_cache):
    cache, _ = redis_cache

    assert await cache.claim_once("nonce:svc_x:abc", 600) is True
    assert await cache.claim_once("nonce:svc_x:abc", 600) is False
    assert await cache.claim_once("nonce:svc_x:def", 600) is True


async def test_keys_are_namespaced_because_the_box_hosts_other_projects(redis_cache):
    cache, stub = redis_cache

    await cache.set("sess:abc", "1")

    assert list(stub.store) == ["synora:sess:abc"]


async def test_a_counter_sets_its_window_only_on_the_first_hit(redis_cache):
    """Re-setting the TTL on every hit turns a fixed window into one that never
    expires under sustained load — which is how a rate limiter locks someone
    out permanently."""
    cache, stub = redis_cache

    for _ in range(5):
        total = await cache.increment("rl:user:login", 60)

    assert total == 5
    assert stub.calls.count("expire") == 1


async def test_a_round_trip(redis_cache):
    cache, _ = redis_cache

    await cache.set("k", "v", ttl_seconds=30)
    assert await cache.get("k") == "v"
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_deleting_nothing_does_not_call_redis(redis_cache):
    cache, stub = redis_cache

    await cache.delete()

    assert stub.calls == []


# --- degradation -----------------------------------------------------------


async def test_when_redis_dies_mid_request_the_guard_still_fails_open(redis_cache):
    cache, stub = redis_cache
    stub.fail = True

    assert await cache.claim_once("nonce:svc_x:abc", 600) is True
    assert cache.is_available is False


async def test_a_dead_redis_reads_as_absent_rather_than_as_an_error(redis_cache):
    """The whole point of the fallback: an outage should look like a state the
    app is already designed for, not a new failure mode."""
    cache, stub = redis_cache
    stub.fail = True

    assert await cache.get("k") is None
    assert await cache.increment("rl", 60) == 0
    await cache.set("k", "v")
    await cache.delete("k")


async def test_the_outage_is_logged_once_and_the_recovery_too(redis_cache, caplog):
    """Logged per outage, not per request. A Redis that is down is down for
    thousands of requests, and drowning the log is how the next problem goes
    unnoticed."""
    cache, stub = redis_cache
    stub.fail = True

    with caplog.at_level("ERROR", logger="synora.cache"):
        for _ in range(10):
            await cache.get("k")

    assert len([r for r in caplog.records if "unavailable" in r.message]) == 1

    stub.fail = False
    with caplog.at_level("INFO", logger="synora.cache"):
        await cache.get("k")
    assert any("answering again" in r.message for r in caplog.records)
    assert cache.is_available is True


# --- selection -------------------------------------------------------------


async def test_the_app_runs_on_the_null_cache_when_no_url_is_set(monkeypatch):
    monkeypatch.setattr(cache_module, "_cache", None)

    assert isinstance(get_cache(), NullCache)


async def test_the_cache_is_built_once(monkeypatch):
    """Lazily rather than in the lifespan, because the lifespan does not run
    under ASGITransport and a lifespan-built client would be None in tests."""
    monkeypatch.setattr(cache_module, "_cache", None)

    assert get_cache() is get_cache()
