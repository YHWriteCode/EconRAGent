from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError
from redis.asyncio import Redis

from lightrag_fork.utils import logger

from .lock_backend import (
    LockBackend,
    LockBackendUnavailable,
    LockLease,
)


ACQUIRE_LUA = """
if redis.call("exists", KEYS[1]) == 0 then
  redis.call("psetex", KEYS[1], ARGV[2], ARGV[1])
  return 1
end
return 0
"""

RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  redis.call("pexpire", KEYS[1], ARGV[2])
  return 1
end
return 0
"""

RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  redis.call("del", KEYS[1])
  return 1
end
return 0
"""


@dataclass
class RedisLockOptions:
    ttl_s: int
    wait_timeout_s: float | None
    retry_interval_s: float
    auto_renew: bool


class RedisLockManager:
    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "lightrag:lock",
        renew_interval_s: float | None = None,
        max_retries: int | None = None,
    ):
        self.redis_url = redis_url
        self.key_prefix = key_prefix.rstrip(":")
        self.renew_interval_s = (
            renew_interval_s if renew_interval_s is None else max(renew_interval_s, 0.01)
        )
        self.max_retries = max_retries if max_retries is None else max(max_retries, 0)
        self._client: Redis | None = None
        self._scripts_loaded = False
        self._acquire_sha: str | None = None
        self._renew_sha: str | None = None
        self._release_sha: str | None = None

    async def _get_client(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    async def _ensure_scripts(self) -> None:
        if self._scripts_loaded:
            return
        client = await self._get_client()
        self._acquire_sha = await client.script_load(ACQUIRE_LUA)
        self._renew_sha = await client.script_load(RENEW_LUA)
        self._release_sha = await client.script_load(RELEASE_LUA)
        self._scripts_loaded = True

    def build_redis_key(self, key: str) -> str:
        return f"{self.key_prefix}:{key}"

    async def acquire(
        self,
        key: str,
        owner: str,
        options: RedisLockOptions,
    ) -> LockLease | None:
        await self._ensure_scripts()
        client = await self._get_client()
        assert self._acquire_sha is not None

        token = uuid.uuid4().hex
        now = time.time()
        value_payload = {
            "token": token,
            "owner": owner,
            "ts": now,
        }
        value = json.dumps(value_payload, ensure_ascii=True, separators=(",", ":"))
        ttl_ms = int(max(options.ttl_s, 1) * 1000)
        redis_key = self.build_redis_key(key)

        deadline = None
        if options.wait_timeout_s is not None:
            deadline = time.monotonic() + max(options.wait_timeout_s, 0.0)

        attempts = 0
        while True:
            attempts += 1
            acquired = await client.evalsha(
                self._acquire_sha,
                1,
                redis_key,
                value,
                ttl_ms,
            )
            if acquired == 1:
                lease = LockLease(
                    key=key,
                    token=token,
                    owner_id=owner,
                    acquired_at=now,
                    ttl_s=options.ttl_s,
                    backend="redis",
                    backend_data={
                        "redis_key": redis_key,
                        "value": value,
                    },
                )
                if options.auto_renew:
                    lease.renew_task = asyncio.create_task(self._renew_loop(lease))
                return lease

            if deadline is not None and time.monotonic() >= deadline:
                return None

            if self.max_retries is not None and attempts >= self.max_retries:
                return None

            await asyncio.sleep(max(options.retry_interval_s, 0.01))

    async def renew(self, lease: LockLease, ttl_s: int) -> bool:
        if lease.closed:
            return False
        await self._ensure_scripts()
        client = await self._get_client()
        assert self._renew_sha is not None

        redis_key = lease.backend_data["redis_key"]
        value = lease.backend_data["value"]
        ttl_ms = int(max(ttl_s, 1) * 1000)
        renewed = await client.evalsha(self._renew_sha, 1, redis_key, value, ttl_ms)
        return renewed == 1

    async def release(self, lease: LockLease) -> bool:
        if lease.closed:
            return True

        renew_task = lease.renew_task
        if renew_task is not None and not renew_task.done():
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass

        await self._ensure_scripts()
        client = await self._get_client()
        assert self._release_sha is not None

        redis_key = lease.backend_data["redis_key"]
        value = lease.backend_data["value"]
        released = await client.evalsha(self._release_sha, 1, redis_key, value)
        lease.closed = True
        return released == 1

    async def is_locked(self, key: str) -> bool:
        client = await self._get_client()
        return bool(await client.exists(self.build_redis_key(key)))

    async def acquire_many(
        self,
        keys: list[str],
        owner: str,
        options: RedisLockOptions,
    ) -> list[LockLease] | None:
        leases: list[LockLease] = []
        for key in sorted(keys):
            lease = await self.acquire(key, owner, options)
            if lease is None:
                await self.release_many(leases)
                return None
            leases.append(lease)
        return leases

    async def release_many(self, leases: list[LockLease]) -> None:
        for lease in reversed(leases):
            try:
                await self.release(lease)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning(f"Failed releasing lock {lease.key}: {e}")

    async def _renew_loop(self, lease: LockLease) -> None:
        interval = (
            self.renew_interval_s
            if self.renew_interval_s is not None
            else max(1.0, lease.ttl_s / 3.0)
        )
        failures = 0
        while not lease.closed:
            await asyncio.sleep(interval)
            try:
                ok = await self.renew(lease, lease.ttl_s)
            except Exception as e:  # pragma: no cover - defensive logging
                logger.warning(f"Lock renew failed for {lease.key}: {e}")
                ok = False

            if ok:
                failures = 0
                continue
            failures += 1
            if failures >= 2:
                lease.lost = True
                logger.error(
                    f"Lost distributed lock lease for key={lease.key}, owner={lease.owner_id}"
                )
                return


class RedisLockBackend(LockBackend):
    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "lightrag:lock",
        fail_mode: str = "strict",
        fallback_backend: LockBackend | None = None,
        renew_interval_s: float | None = None,
        max_retries: int | None = None,
    ):
        self.fail_mode = fail_mode
        self.fallback_backend = fallback_backend
        self.manager = RedisLockManager(
            redis_url=redis_url,
            key_prefix=key_prefix,
            renew_interval_s=renew_interval_s,
            max_retries=max_retries,
        )

    async def acquire(
        self,
        key: str,
        owner: str,
        ttl_s: int,
        wait_timeout_s: float | None,
        retry_interval_s: float,
        auto_renew: bool,
    ) -> LockLease | None:
        options = RedisLockOptions(
            ttl_s=ttl_s,
            wait_timeout_s=wait_timeout_s,
            retry_interval_s=retry_interval_s,
            auto_renew=auto_renew,
        )
        try:
            return await self.manager.acquire(key, owner, options)
        except RedisError as e:
            if self.fail_mode == "fallback_local" and self.fallback_backend is not None:
                logger.warning(
                    "Redis lock backend unavailable, falling back to local lock backend: %s",
                    e,
                )
                return await self.fallback_backend.acquire(
                    key=key,
                    owner=owner,
                    ttl_s=ttl_s,
                    wait_timeout_s=wait_timeout_s,
                    retry_interval_s=retry_interval_s,
                    auto_renew=auto_renew,
                )
            raise LockBackendUnavailable(
                f"Redis lock backend unavailable (key={key}): {e}"
            ) from e

    async def release(self, lease: LockLease) -> bool:
        if lease.backend != "redis" and self.fallback_backend is not None:
            return await self.fallback_backend.release(lease)
        try:
            return await self.manager.release(lease)
        except RedisError as e:
            if self.fail_mode == "fallback_local":
                logger.warning(f"Redis release failed (key={lease.key}): {e}")
                lease.closed = True
                return False
            raise LockBackendUnavailable(
                f"Redis lock release failed (key={lease.key}): {e}"
            ) from e

    async def renew(self, lease: LockLease, ttl_s: int) -> bool:
        if lease.backend != "redis" and self.fallback_backend is not None:
            return await self.fallback_backend.renew(lease, ttl_s)
        try:
            return await self.manager.renew(lease, ttl_s)
        except RedisError as e:
            if self.fail_mode == "fallback_local":
                logger.warning(f"Redis renew failed (key={lease.key}): {e}")
                return False
            raise LockBackendUnavailable(
                f"Redis lock renew failed (key={lease.key}): {e}"
            ) from e

    async def is_locked(self, key: str) -> bool:
        try:
            return await self.manager.is_locked(key)
        except RedisError as e:
            if self.fail_mode == "fallback_local" and self.fallback_backend is not None:
                return await self.fallback_backend.is_locked(key)
            raise LockBackendUnavailable(
                f"Redis lock status check failed (key={key}): {e}"
            ) from e
