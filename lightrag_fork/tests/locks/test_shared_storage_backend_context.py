from __future__ import annotations

import asyncio
import time

import pytest

from lightrag_fork.kg.lock_backend import LockBackend, LockLease, LockLostError
from lightrag_fork.kg.shared_storage import _BackendKeyedLockContext


def _make_lease(key: str, owner: str = "unit-owner") -> LockLease:
    return LockLease(
        key=key,
        token=f"token-{key}",
        owner_id=owner,
        acquired_at=time.time(),
        ttl_s=10,
        backend="mock",
    )


@pytest.mark.asyncio
async def test_backend_context_releases_lock_when_body_raises(mocker):
    # Arrange
    lease = _make_lease("workspace-a:pipeline:runtime")
    backend = mocker.Mock(spec=LockBackend)
    backend.acquire = mocker.AsyncMock(return_value=lease)
    backend.release = mocker.AsyncMock(return_value=True)
    backend.renew = mocker.AsyncMock(return_value=True)
    backend.is_locked = mocker.AsyncMock(return_value=False)
    ctx = _BackendKeyedLockContext(
        backend=backend,
        namespace="workspace-a",
        keys=["pipeline:runtime"],
        ttl_s=10,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
        enable_logging=False,
    )

    # Act
    with pytest.raises(RuntimeError, match="body failure"):
        async with ctx:
            raise RuntimeError("body failure")

    # Assert
    backend.release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_backend_context_interrupts_when_lease_lost(mocker, monkeypatch):
    # Arrange
    monkeypatch.setenv("LIGHTRAG_LOCK_LOST_CHECK_INTERVAL_S", "0.01")
    lease = _make_lease("workspace-a:pipeline:runtime")
    backend = mocker.Mock(spec=LockBackend)
    backend.acquire = mocker.AsyncMock(return_value=lease)
    backend.release = mocker.AsyncMock(return_value=True)
    backend.renew = mocker.AsyncMock(return_value=True)
    backend.is_locked = mocker.AsyncMock(return_value=False)
    ctx = _BackendKeyedLockContext(
        backend=backend,
        namespace="workspace-a",
        keys=["pipeline:runtime"],
        ttl_s=10,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
        enable_logging=False,
    )

    # Act
    with pytest.raises(LockLostError):
        async with ctx:
            lease.lost = True
            await asyncio.sleep(0.05)

    # Assert
    backend.release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_backend_context_rolls_back_acquired_keys_when_multi_key_timeout(mocker):
    # Arrange
    lease_first = _make_lease("workspace-a:key-a")
    backend = mocker.Mock(spec=LockBackend)
    backend.acquire = mocker.AsyncMock(side_effect=[lease_first, None])
    backend.release = mocker.AsyncMock(return_value=True)
    backend.renew = mocker.AsyncMock(return_value=True)
    backend.is_locked = mocker.AsyncMock(return_value=False)
    ctx = _BackendKeyedLockContext(
        backend=backend,
        namespace="workspace-a",
        keys=["key-a", "key-b"],
        ttl_s=10,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
        enable_logging=False,
    )

    # Act
    with pytest.raises(TimeoutError):
        await ctx.__aenter__()

    # Assert
    assert backend.acquire.await_count == 2
    backend.release.assert_awaited_once_with(lease_first)
    assert ctx._leases is None
