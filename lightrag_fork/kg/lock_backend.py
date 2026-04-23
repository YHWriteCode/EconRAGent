from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class LockBackendError(RuntimeError):
    """Base exception for lock backend failures."""


class LockBackendUnavailable(LockBackendError):
    """Raised when distributed lock backend is unavailable."""


class LockLostError(LockBackendError):
    """Raised when a previously acquired lock lease is lost."""


@dataclass
class LockLease:
    """Represents a lock lease acquired from a backend."""

    key: str
    token: str
    owner_id: str
    acquired_at: float
    ttl_s: int
    backend: str
    renew_task: asyncio.Task | None = None
    backend_data: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    lost: bool = False


class LockBackend(ABC):
    @abstractmethod
    async def acquire(
        self,
        key: str,
        owner: str,
        ttl_s: int,
        wait_timeout_s: float | None,
        retry_interval_s: float,
        auto_renew: bool,
    ) -> LockLease | None:
        """Acquire lock and return lease, or None on contention timeout."""

    @abstractmethod
    async def release(self, lease: LockLease) -> bool:
        """Release lock lease. Repeated release should be idempotent."""

    @abstractmethod
    async def renew(self, lease: LockLease, ttl_s: int) -> bool:
        """Renew lock lease ttl."""

    @abstractmethod
    async def is_locked(self, key: str) -> bool:
        """Check whether key is currently locked."""


class LocalLockBackend(LockBackend):
    """Adapter that reuses existing local KeyedUnifiedLock implementation."""

    def __init__(
        self,
        local_context_factory: Callable[..., Any],
        namespace: str = "LocalLockBackend",
    ):
        self._local_context_factory = local_context_factory
        self._namespace = namespace

    async def acquire(
        self,
        key: str,
        owner: str,
        ttl_s: int,
        wait_timeout_s: float | None,
        retry_interval_s: float,
        auto_renew: bool,
    ) -> LockLease | None:
        del owner, ttl_s, retry_interval_s, auto_renew
        ctx = self._local_context_factory(
            self._namespace,
            [key],
            enable_logging=False,
        )
        try:
            if wait_timeout_s is None:
                await ctx.__aenter__()
            elif wait_timeout_s <= 0:
                enter_task = asyncio.create_task(ctx.__aenter__())
                await asyncio.sleep(0)
                if not enter_task.done():
                    enter_task.cancel()
                    try:
                        await enter_task
                    except BaseException:
                        pass
                    return None
                await enter_task
            else:
                await asyncio.wait_for(ctx.__aenter__(), timeout=wait_timeout_s)
        except asyncio.TimeoutError:
            return None

        return LockLease(
            key=key,
            token=uuid.uuid4().hex,
            owner_id="local",
            acquired_at=time.time(),
            ttl_s=0,
            backend="local",
            backend_data={"ctx": ctx},
        )

    async def release(self, lease: LockLease) -> bool:
        if lease.closed:
            return True
        ctx = lease.backend_data.get("ctx")
        if ctx is None:
            lease.closed = True
            return True
        try:
            await ctx.__aexit__(None, None, None)
            return True
        finally:
            lease.closed = True

    async def renew(self, lease: LockLease, ttl_s: int) -> bool:
        del ttl_s
        return not lease.closed and not lease.lost

    async def is_locked(self, key: str) -> bool:
        del key
        # Existing local lock manager does not expose robust lock state check.
        return False
