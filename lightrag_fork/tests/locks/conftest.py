from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Callable

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PARENT = REPO_ROOT.parent
if str(REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(REPO_PARENT))


@pytest_asyncio.fixture
async def fake_redis() -> FakeRedis:
    redis = FakeRedis(decode_responses=True)
    try:
        yield redis
    finally:
        close_coro = getattr(redis, "aclose", None)
        if callable(close_coro):
            await close_coro()
        else:
            close_fn = getattr(redis, "close", None)
            if callable(close_fn):
                close_fn()


@pytest.fixture
def local_backend():
    from lightrag_fork.kg.lock_backend import LocalLockBackend

    class _SingleKeyLockContext:
        def __init__(self, lock: asyncio.Lock):
            self._lock = lock

        async def __aenter__(self):
            await self._lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self._lock.locked():
                self._lock.release()

    locks: dict[str, asyncio.Lock] = {}

    def _context_factory(namespace: str, keys: list[str], enable_logging: bool):
        del enable_logging
        lock_key = f"{namespace}:{'|'.join(sorted(keys))}"
        lock = locks.setdefault(lock_key, asyncio.Lock())
        return _SingleKeyLockContext(lock)

    return LocalLockBackend(local_context_factory=_context_factory)


@pytest.fixture
def redis_backend_factory(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., object]:
    from lightrag_fork.kg.redis_lock_backend import RedisLockBackend

    def _factory(fail_mode: str = "strict") -> RedisLockBackend:
        backend = RedisLockBackend(
            redis_url="redis://unit-test/0",
            key_prefix="lightrag:test:lock",
            fail_mode=fail_mode,
        )

        async def _fake_get_client() -> FakeRedis:
            return fake_redis

        monkeypatch.setattr(backend.manager, "_get_client", _fake_get_client)
        return backend

    return _factory
