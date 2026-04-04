from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from lightrag_fork.kg.redis_lock_backend import RedisLockBackend
from lightrag_fork.utils import compute_mdhash_id

from kg_agent.config import SchedulerConfig
from kg_agent.crawler.crawl_state_store import (
    CrawlStateRecord,
    CrawlStateStore,
    JsonCrawlStateStore,
)
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
from kg_agent.crawler.source_registry import (
    JsonSourceRegistry,
    MonitoredSource,
    SourceRegistry,
)

logger = logging.getLogger(__name__)

RagResolver = Callable[[str | None], Any | Awaitable[Any]]


@dataclass
class SchedulerCoordinationLease:
    key: str
    backend: str
    payload: Any = None


class SchedulerCoordinator:
    backend_name = "local"

    async def acquire(
        self,
        key: str,
        *,
        ttl_s: int,
        wait_timeout_s: float | None = 0.0,
    ) -> SchedulerCoordinationLease | None:
        raise NotImplementedError

    async def release(self, lease: SchedulerCoordinationLease | None) -> None:
        raise NotImplementedError


class LocalSchedulerCoordinator(SchedulerCoordinator):
    backend_name = "local"

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(
        self,
        key: str,
        *,
        ttl_s: int,
        wait_timeout_s: float | None = 0.0,
    ) -> SchedulerCoordinationLease | None:
        del ttl_s, wait_timeout_s
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return None
        await lock.acquire()
        return SchedulerCoordinationLease(
            key=key,
            backend=self.backend_name,
            payload=lock,
        )

    async def release(self, lease: SchedulerCoordinationLease | None) -> None:
        if lease is None or not isinstance(lease.payload, asyncio.Lock):
            return
        if lease.payload.locked():
            lease.payload.release()


class RedisSchedulerCoordinator(SchedulerCoordinator):
    backend_name = "redis"

    def __init__(
        self,
        *,
        redis_url: str,
        key_prefix: str = "kg_agent:scheduler",
        local_fallback: SchedulerCoordinator | None = None,
    ):
        self._owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"
        self._backend = RedisLockBackend(
            redis_url=redis_url,
            key_prefix=key_prefix,
            fail_mode="strict",
        )
        self._local_fallback = local_fallback

    async def acquire(
        self,
        key: str,
        *,
        ttl_s: int,
        wait_timeout_s: float | None = 0.0,
    ) -> SchedulerCoordinationLease | None:
        try:
            lease = await self._backend.acquire(
                key=key,
                owner=self._owner,
                ttl_s=ttl_s,
                wait_timeout_s=wait_timeout_s,
                retry_interval_s=0.05,
                auto_renew=True,
            )
        except Exception as exc:
            if self._local_fallback is None:
                raise
            logger.warning(
                "Redis scheduler coordination failed for key '%s', falling back to local coordination: %s",
                key,
                exc,
            )
            return await self._local_fallback.acquire(
                key,
                ttl_s=ttl_s,
                wait_timeout_s=wait_timeout_s,
            )

        if lease is None:
            return None
        return SchedulerCoordinationLease(
            key=key,
            backend=self.backend_name,
            payload=lease,
        )

    async def release(self, lease: SchedulerCoordinationLease | None) -> None:
        if lease is None:
            return
        if lease.backend == "local" and self._local_fallback is not None:
            await self._local_fallback.release(lease)
            return
        if lease.payload is None:
            return
        await self._backend.release(lease.payload)


def build_scheduler_coordinator(
    config: SchedulerConfig | None,
) -> SchedulerCoordinator:
    config_obj = config or SchedulerConfig()
    backend = (config_obj.coordination_backend or "auto").strip().lower()
    redis_url = (config_obj.coordination_redis_url or "").strip()
    local = LocalSchedulerCoordinator()
    if backend == "local":
        return local
    if backend == "redis":
        if not redis_url:
            raise RuntimeError(
                "KG_AGENT_SCHEDULER_COORDINATION_BACKEND=redis requires KG_AGENT_SCHEDULER_COORDINATION_REDIS_URL or REDIS_URI"
            )
        return RedisSchedulerCoordinator(redis_url=redis_url, local_fallback=local)
    if backend != "auto":
        raise RuntimeError(
            f"Unsupported scheduler coordination backend: {backend}"
        )
    if redis_url:
        return RedisSchedulerCoordinator(redis_url=redis_url, local_fallback=local)
    return local


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class IngestScheduler:
    def __init__(
        self,
        *,
        rag_provider: RagResolver,
        crawler_adapter: Crawl4AIAdapter,
        source_registry: SourceRegistry | None = None,
        state_store: CrawlStateStore | None = None,
        enabled: bool = False,
        check_interval_seconds: int = 60,
        coordinator: SchedulerCoordinator | None = None,
        coordination_ttl_seconds: int = 120,
        enable_leader_election: bool = False,
        loop_lease_key: str = "scheduler:loop",
    ):
        self._rag_provider = rag_provider
        self.crawler_adapter = crawler_adapter
        self.source_registry = source_registry or JsonSourceRegistry()
        self.state_store = state_store or JsonCrawlStateStore()
        self.enabled = bool(enabled)
        self.check_interval_seconds = max(1, int(check_interval_seconds))
        self.coordinator = coordinator or LocalSchedulerCoordinator()
        self.coordination_ttl_seconds = max(5, int(coordination_ttl_seconds))
        self.enable_leader_election = bool(enable_leader_election)
        self.loop_lease_key = (loop_lease_key or "scheduler:loop").strip() or "scheduler:loop"

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._status_lock = asyncio.Lock()
        self._started_at: str | None = None
        self._last_tick_at: str | None = None
        self._last_error: str | None = None
        self._loop_iterations = 0
        self._leader_role = "disabled"
        self._loop_leader_lease: SchedulerCoordinationLease | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._started_at = _utcnow_iso()
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._run_loop(), name="kg-agent-ingest-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            await self._release_loop_leader_lease()
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            await self._release_loop_leader_lease()

    async def list_sources(self) -> list[MonitoredSource]:
        return await self.source_registry.list_sources()

    async def add_source(self, source: MonitoredSource) -> MonitoredSource:
        stored = await self.source_registry.upsert_source(source)
        if await self.state_store.get_record(stored.source_id) is None:
            await self.state_store.put_record(CrawlStateRecord(source_id=stored.source_id))
        return stored

    async def remove_source(self, source_id: str) -> bool:
        removed = await self.source_registry.remove_source(source_id)
        if removed:
            await self.state_store.remove_record(source_id)
            self._source_locks.pop(source_id, None)
        return removed

    async def trigger_now(self, source_id: str) -> dict[str, Any]:
        source = await self.source_registry.get_source(source_id)
        if source is None:
            return {"status": "not_found", "source_id": source_id}
        return await self._poll_source(source)

    async def get_status(self) -> dict[str, Any]:
        sources = await self.source_registry.list_sources()
        source_items: list[dict[str, Any]] = []
        for source in sources:
            record = await self.state_store.get_record(source.source_id)
            effective_interval_seconds = self._effective_interval_seconds(source, record)
            source_items.append(
                {
                    **source.to_dict(),
                    "last_crawled_at": None if record is None else record.last_crawled_at,
                    "last_status": "never_run" if record is None else record.last_status,
                    "consecutive_failures": 0 if record is None else record.consecutive_failures,
                    "total_ingested_count": 0 if record is None else record.total_ingested_count,
                    "last_error": None if record is None else record.last_error,
                    "effective_interval_seconds": effective_interval_seconds,
                    "next_poll_due_in_seconds": self._seconds_until_due(
                        source,
                        record,
                        effective_interval_seconds,
                    ),
                }
            )
        return {
            "configured": True,
            "enabled": self.enabled,
            "running": self._task is not None and not self._task.done(),
            "check_interval_seconds": self.check_interval_seconds,
            "coordination_backend": getattr(self.coordinator, "backend_name", "local"),
            "leader_election_enabled": self.enable_leader_election,
            "leader_role": self._leader_role,
            "loop_lease_key": self.loop_lease_key,
            "started_at": self._started_at,
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
            "loop_iterations": self._loop_iterations,
            "sources_file": self.source_registry.file_path,
            "state_file": self.state_store.file_path,
            "source_count": len(source_items),
            "sources": source_items,
        }

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._run_once()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.check_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.exception("Scheduler loop crashed: %s", exc)
            async with self._status_lock:
                self._last_error = str(exc)

    async def _run_once(self) -> None:
        self._last_tick_at = _utcnow_iso()
        self._loop_iterations += 1
        if not self.enable_leader_election:
            self._leader_role = "disabled"
            await self._poll_due_sources()
            return

        if not await self._ensure_loop_leader_lease():
            self._leader_role = "standby"
            return

        self._leader_role = "leader"
        await self._poll_due_sources()

    async def _ensure_loop_leader_lease(self) -> bool:
        if self._loop_leader_lease is not None:
            return True
        lease = await self.coordinator.acquire(
            self.loop_lease_key,
            ttl_s=self.coordination_ttl_seconds,
            wait_timeout_s=0.0,
        )
        if lease is None:
            return False
        self._loop_leader_lease = lease
        return True

    async def _release_loop_leader_lease(self) -> None:
        lease = self._loop_leader_lease
        self._loop_leader_lease = None
        if lease is None:
            if self.enable_leader_election:
                self._leader_role = "standby"
            return
        await self.coordinator.release(lease)
        if self.enable_leader_election:
            self._leader_role = "standby"

    async def _poll_due_sources(self) -> None:
        sources = await self.source_registry.list_sources()
        for source in sources:
            if not source.enabled:
                continue
            record = await self.state_store.get_record(source.source_id)
            if not self._is_due(source, record):
                continue
            await self._poll_source(source)

    async def _poll_source(self, source: MonitoredSource) -> dict[str, Any]:
        coordination_lease = await self.coordinator.acquire(
            f"source:{source.source_id}",
            ttl_s=self.coordination_ttl_seconds,
            wait_timeout_s=0.0,
        )
        if coordination_lease is None:
            return {
                "status": "busy",
                "source_id": source.source_id,
                "summary": (
                    f"Source '{source.source_id}' is already being polled by another scheduler instance"
                ),
            }

        lock = self._source_locks.setdefault(source.source_id, asyncio.Lock())
        if lock.locked():
            await self.coordinator.release(coordination_lease)
            return {
                "status": "busy",
                "source_id": source.source_id,
                "summary": f"Source '{source.source_id}' is already being polled",
            }

        async with lock:
            now_iso = _utcnow_iso()
            previous = await self.state_store.get_record(source.source_id) or CrawlStateRecord(
                source_id=source.source_id
            )
            hashes = dict(previous.last_content_hashes)
            requested_count = 0
            ingested_count = 0
            success_count = 0
            failure_messages: list[str] = []
            rag = None

            try:
                pages = await self.crawler_adapter.crawl_urls(
                    source.urls,
                    max_pages=source.max_pages,
                )
                requested_count = len(pages)
                for page in pages:
                    page_key = (page.final_url or page.url or "").strip() or page.url
                    if not page.success:
                        failure_messages.append(
                            f"{page_key}: {page.error or 'crawl_failed'}"
                        )
                        continue

                    success_count += 1
                    content = (page.markdown or "").strip()
                    if not content:
                        continue

                    content_hash = compute_mdhash_id(content)
                    old_hash = hashes.get(page_key)
                    hashes[page_key] = content_hash
                    if old_hash == content_hash:
                        continue

                    if rag is None:
                        rag = await self._resolve_rag(source.workspace)
                    await rag.ainsert(
                        input=content,
                        file_paths=page.final_url or page.url,
                    )
                    ingested_count += 1

                last_status = self._build_status(
                    requested_count=requested_count,
                    success_count=success_count,
                    ingested_count=ingested_count,
                    failure_messages=failure_messages,
                )
                record = CrawlStateRecord(
                    source_id=source.source_id,
                    last_crawled_at=now_iso,
                    last_content_hashes=hashes,
                    last_status=last_status,
                    consecutive_failures=0 if success_count > 0 else previous.consecutive_failures + 1,
                    total_ingested_count=previous.total_ingested_count + ingested_count,
                    last_error="; ".join(failure_messages[:3]) or None,
                )
                await self.state_store.put_record(record)
                return {
                    "status": last_status,
                    "source_id": source.source_id,
                    "requested_count": requested_count,
                    "success_count": success_count,
                    "ingested_count": ingested_count,
                    "summary": (
                        f"Polled {requested_count} page(s), {success_count} succeeded, "
                        f"{ingested_count} ingested"
                    ),
                }
            except Exception as exc:  # pragma: no cover
                logger.exception("Polling source '%s' failed: %s", source.source_id, exc)
                failure_messages.append(str(exc))
                record = CrawlStateRecord(
                    source_id=source.source_id,
                    last_crawled_at=now_iso,
                    last_content_hashes=hashes,
                    last_status="failed",
                    consecutive_failures=previous.consecutive_failures + 1,
                    total_ingested_count=previous.total_ingested_count,
                    last_error=str(exc),
                )
                await self.state_store.put_record(record)
                async with self._status_lock:
                    self._last_error = str(exc)
                return {
                    "status": "failed",
                    "source_id": source.source_id,
                    "requested_count": requested_count,
                    "success_count": success_count,
                    "ingested_count": ingested_count,
                    "summary": str(exc),
                }
            finally:
                await self.coordinator.release(coordination_lease)

    async def _resolve_rag(self, workspace: str | None):
        resolved = self._rag_provider(workspace)
        if asyncio.iscoroutine(resolved):
            return await resolved
        return resolved

    def _is_due(
        self,
        source: MonitoredSource,
        record: CrawlStateRecord | None,
    ) -> bool:
        if record is None or not record.last_crawled_at:
            return True
        last_crawled_at = _parse_iso8601(record.last_crawled_at)
        if last_crawled_at is None:
            return True
        elapsed_seconds = (datetime.now(timezone.utc) - last_crawled_at).total_seconds()
        return elapsed_seconds >= self._effective_interval_seconds(source, record)

    @staticmethod
    def _build_status(
        *,
        requested_count: int,
        success_count: int,
        ingested_count: int,
        failure_messages: list[str],
    ) -> str:
        if success_count == 0:
            return "failed"
        if failure_messages:
            return "partial_success"
        if requested_count > 0 and ingested_count == 0:
            return "no_change"
        return "success"

    @staticmethod
    def _effective_interval_seconds(
        source: MonitoredSource,
        record: CrawlStateRecord | None,
    ) -> int:
        base_interval = max(1, source.interval_seconds)
        if record is None or record.consecutive_failures < 3:
            return base_interval
        extra_failures = record.consecutive_failures - 2
        multiplier = min(8, 2 ** extra_failures)
        return base_interval * multiplier

    @staticmethod
    def _seconds_until_due(
        source: MonitoredSource,
        record: CrawlStateRecord | None,
        effective_interval_seconds: int,
    ) -> int:
        if record is None or not record.last_crawled_at:
            return 0
        last_crawled_at = _parse_iso8601(record.last_crawled_at)
        if last_crawled_at is None:
            return 0
        elapsed_seconds = (datetime.now(timezone.utc) - last_crawled_at).total_seconds()
        return max(0, int(effective_interval_seconds - elapsed_seconds))


CrawlScheduler = IngestScheduler
