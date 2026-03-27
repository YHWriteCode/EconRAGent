from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from lightrag_fork.kg.lock_backend import LockLease, LockLostError
from lightrag_fork.kg.shared_storage import _BackendKeyedLockContext


@pytest.mark.asyncio
async def test_acquire_release_renew_roundtrip_with_fake_redis(redis_backend_factory):
    # Arrange
    backend = redis_backend_factory(fail_mode="strict")

    # Act
    lease = await backend.acquire(
        key="workspace-a:pipeline:runtime",
        owner="unit-test-owner",
        ttl_s=5,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
    )
    assert lease is not None
    renewed = await backend.renew(lease, ttl_s=5)
    locked_before_release = await backend.is_locked("workspace-a:pipeline:runtime")
    released = await backend.release(lease)
    locked_after_release = await backend.is_locked("workspace-a:pipeline:runtime")

    # Assert
    assert renewed is True
    assert locked_before_release is True
    assert released is True
    assert locked_after_release is False


@pytest.mark.asyncio
async def test_release_with_wrong_token_does_not_unlock(redis_backend_factory):
    # Arrange
    backend = redis_backend_factory(fail_mode="strict")
    lease = await backend.acquire(
        key="workspace-a:pipeline:enqueue",
        owner="owner-a",
        ttl_s=5,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
    )
    assert lease is not None
    forged_lease = LockLease(
        key=lease.key,
        token="forged-token",
        owner_id="attacker",
        acquired_at=time.time(),
        ttl_s=5,
        backend="redis",
        backend_data={
            "redis_key": lease.backend_data["redis_key"],
            "value": '{"token":"forged-token","owner":"attacker","ts":0}',
        },
    )

    # Act
    forged_release_ok = await backend.release(forged_lease)
    still_locked = await backend.is_locked("workspace-a:pipeline:enqueue")
    await backend.release(lease)
    finally_unlocked = await backend.is_locked("workspace-a:pipeline:enqueue")

    # Assert
    assert forged_release_ok is False
    assert still_locked is True
    assert finally_unlocked is False


@pytest.mark.asyncio
async def test_auto_renew_task_is_cancelled_on_release(redis_backend_factory):
    # Arrange
    backend = redis_backend_factory(fail_mode="strict")
    backend.manager.renew_interval_s = 0.01
    lease = await backend.acquire(
        key="workspace-a:graph:entity:GDP",
        owner="owner-a",
        ttl_s=5,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=True,
    )
    assert lease is not None
    assert lease.renew_task is not None

    # Act
    await backend.release(lease)

    # Assert
    assert lease.closed is True
    assert lease.renew_task.done() is True


@pytest.mark.asyncio
async def test_renew_loop_marks_lease_as_lost_after_retries(redis_backend_factory, monkeypatch):
    # Arrange
    backend = redis_backend_factory(fail_mode="strict")
    lease = await backend.acquire(
        key="workspace-a:graph:relation:GDP:CPI",
        owner="owner-a",
        ttl_s=2,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
    )
    assert lease is not None
    backend.manager.renew_interval_s = 0.01
    monkeypatch.setattr(
        backend.manager,
        "renew",
        AsyncMock(side_effect=[False, False]),
    )

    # Act
    await backend.manager._renew_loop(lease)

    # Assert
    assert lease.lost is True
    await backend.release(lease)


@pytest.mark.asyncio
async def test_lock_lost_error_triggered_by_backend_context_checkpoint(redis_backend_factory):
    # Arrange
    backend = redis_backend_factory(fail_mode="strict")
    ctx = _BackendKeyedLockContext(
        backend=backend,
        namespace="workspace-a",
        keys=["pipeline:runtime"],
        ttl_s=5,
        wait_timeout_s=0,
        retry_interval_s=0.01,
        auto_renew=False,
        enable_logging=False,
    )

    # Act
    with pytest.raises(LockLostError):
        async with ctx:
            assert ctx._leases is not None
            ctx._leases[0].lost = True
            ctx.raise_if_lost()

    # Assert
    assert ctx._leases is None
