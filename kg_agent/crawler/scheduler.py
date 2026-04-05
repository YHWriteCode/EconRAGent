from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from lightrag_fork.kg.redis_lock_backend import RedisLockBackend
from lightrag_fork.utils import compute_mdhash_id

from kg_agent.config import SchedulerConfig
from kg_agent.crawler.crawl_state_store import (
    CrawlStateRecord,
    CrawlStateStore,
    JsonCrawlStateStore,
)
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter, DiscoveredUrl
from kg_agent.crawler.source_registry import (
    JsonSourceRegistry,
    MonitoredSource,
    SourceRegistry,
)

logger = logging.getLogger(__name__)

RagResolver = Callable[[str | None], Any | Awaitable[Any]]
MAX_FEED_NO_CHANGE_MULTIPLIER = 4


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
                    **source.to_public_dict(),
                    "last_crawled_at": None if record is None else record.last_crawled_at,
                    "last_status": "never_run" if record is None else record.last_status,
                    "consecutive_failures": 0 if record is None else record.consecutive_failures,
                    "consecutive_no_change": 0
                    if record is None
                    else record.consecutive_no_change,
                    "tracked_item_count": 0
                    if record is None
                    else len(record.last_content_hashes),
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
            current_feed_item_keys: list[str] = []
            current_feed_item_published_at: dict[str, str] = {}
            successful_feed_item_keys: list[str] = []
            feed_discovered_count = 0
            feed_filtered_count = 0
            rag = None

            try:
                pages, crawl_metadata = await self._collect_pages_for_source(source)
                requested_count = len(pages)
                current_feed_item_keys = list(
                    crawl_metadata.get("feed_item_keys", [])
                )
                current_feed_item_published_at = {
                    str(key): str(value).strip()
                    for key, value in dict(
                        crawl_metadata.get("feed_item_published_at", {})
                    ).items()
                    if str(key).strip() and str(value).strip()
                }
                feed_discovered_count = int(
                    crawl_metadata.get("feed_discovered_count", 0)
                )
                feed_filtered_count = int(
                    crawl_metadata.get("feed_filtered_count", 0)
                )
                failure_messages.extend(
                    [
                        str(item)
                        for item in crawl_metadata.get("pre_crawl_errors", [])
                        if str(item).strip()
                    ]
                )
                for page in pages:
                    page_key = (page.final_url or page.url or "").strip() or page.url
                    if not page.success:
                        failure_messages.append(
                            f"{page_key}: {page.error or 'crawl_failed'}"
                        )
                        continue

                    success_count += 1
                    if source.resolved_source_type() == "feed":
                        successful_feed_item_keys.append(page_key)
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

                recent_item_keys = previous.recent_item_keys
                item_published_at = previous.item_published_at
                if source.resolved_source_type() == "feed":
                    recent_item_keys = self._merge_recent_item_keys(
                        successful_feed_item_keys,
                        previous.recent_item_keys,
                    )
                    item_published_at = self._merge_item_published_at(
                        previous.item_published_at,
                        current_feed_item_published_at,
                    )
                    hashes, recent_item_keys, item_published_at = self._apply_feed_retention(
                        source=source,
                        hashes=hashes,
                        recent_item_keys=recent_item_keys,
                        item_published_at=item_published_at,
                    )

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
                    recent_item_keys=recent_item_keys,
                    item_published_at=item_published_at,
                    last_status=last_status,
                    consecutive_failures=(
                        previous.consecutive_failures + 1
                        if last_status == "failed"
                        else 0
                    ),
                    consecutive_no_change=(
                        previous.consecutive_no_change + 1
                        if last_status == "no_change"
                        else 0
                    ),
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
                    "feed_discovered_count": feed_discovered_count,
                    "feed_filtered_count": feed_filtered_count,
                    "tracked_item_count": len(hashes),
                    "summary": self._build_poll_summary(
                        source=source,
                        requested_count=requested_count,
                        success_count=success_count,
                        ingested_count=ingested_count,
                        feed_discovered_count=feed_discovered_count,
                        feed_filtered_count=feed_filtered_count,
                    ),
                }
            except Exception as exc:  # pragma: no cover
                logger.exception("Polling source '%s' failed: %s", source.source_id, exc)
                failure_messages.append(str(exc))
                record = CrawlStateRecord(
                    source_id=source.source_id,
                    last_crawled_at=now_iso,
                    last_content_hashes=hashes,
                    recent_item_keys=previous.recent_item_keys,
                    item_published_at=previous.item_published_at,
                    last_status="failed",
                    consecutive_failures=previous.consecutive_failures + 1,
                    consecutive_no_change=0,
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
                    "feed_discovered_count": feed_discovered_count,
                    "feed_filtered_count": feed_filtered_count,
                    "tracked_item_count": len(hashes),
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
            if requested_count == 0 and not failure_messages:
                return "no_change"
            return "failed"
        if failure_messages:
            return "partial_success"
        if requested_count > 0 and ingested_count == 0:
            return "no_change"
        return "success"

    async def _collect_pages_for_source(
        self,
        source: MonitoredSource,
    ) -> tuple[list[Any], dict[str, Any]]:
        if source.resolved_source_type() != "feed":
            pages = await self.crawler_adapter.crawl_urls(
                source.urls,
                max_pages=source.max_pages,
            )
            return pages, {}
        return await self._collect_feed_pages(source)

    async def _collect_feed_pages(
        self,
        source: MonitoredSource,
    ) -> tuple[list[Any], dict[str, Any]]:
        page_limit = max(1, source.max_pages)
        discovery_limit = max(page_limit, page_limit * 5)
        priority_active = source.feed_priority.is_active()
        candidate_entries: list[DiscoveredUrl] = []
        pre_crawl_errors: list[str] = []
        feed_discovered_count = 0
        feed_filtered_count = 0
        feed_item_published_at: dict[str, str] = {}

        for feed_url in source.urls:
            if not priority_active and len(candidate_entries) >= page_limit:
                break
            try:
                discovered = await self.crawler_adapter.discover_feed_urls(
                    feed_url,
                    top_k=discovery_limit,
                )
            except Exception as exc:
                pre_crawl_errors.append(f"{feed_url}: {exc}")
                continue

            feed_discovered_count += len(discovered)
            filtered = self._filter_feed_entries(source, discovered)
            feed_filtered_count += max(0, len(discovered) - len(filtered))
            candidate_entries.extend(filtered)

        prioritized_entries = self._prioritize_feed_entries(source, candidate_entries)
        target_urls: list[str] = []
        seen_urls: set[str] = set()
        for item in prioritized_entries:
            normalized_url = (item.url or "").strip()
            if not normalized_url or normalized_url in seen_urls:
                continue
            target_urls.append(normalized_url)
            seen_urls.add(normalized_url)
            if item.published_at:
                feed_item_published_at[normalized_url] = item.published_at
            if len(target_urls) >= page_limit:
                break

        pages = []
        for url in target_urls[:page_limit]:
            pages.append(await self.crawler_adapter.crawl_url(url))
        limited_urls = target_urls[:page_limit]
        return pages, {
            "feed_item_keys": limited_urls,
            "feed_item_published_at": {
                key: value
                for key, value in feed_item_published_at.items()
                if key in limited_urls
            },
            "feed_discovered_count": feed_discovered_count,
            "feed_filtered_count": feed_filtered_count,
            "pre_crawl_errors": pre_crawl_errors,
        }

    @staticmethod
    def _prioritize_feed_entries(
        source: MonitoredSource,
        entries: list[DiscoveredUrl],
    ) -> list[DiscoveredUrl]:
        if len(entries) <= 1:
            return entries
        resolved_mode = source.feed_priority.resolved_mode()
        if resolved_mode == "feed_order":
            return entries

        indexed_entries = list(enumerate(entries))
        if resolved_mode == "published_desc":
            ranked = sorted(
                indexed_entries,
                key=lambda item: (
                    IngestScheduler._published_sort_key(item[1].published_at),
                    -item[0],
                ),
                reverse=True,
            )
            return [entry for _, entry in ranked]

        ranked = sorted(
            indexed_entries,
            key=lambda item: IngestScheduler._feed_entry_priority_key(
                item[1],
                policy=source.feed_priority,
                index=item[0],
            ),
            reverse=True,
        )
        return [entry for _, entry in ranked]

    @staticmethod
    def _filter_feed_entries(
        source: MonitoredSource,
        entries: list[DiscoveredUrl],
    ) -> list[DiscoveredUrl]:
        policy = source.feed_filter
        if not policy.is_active():
            return entries

        filtered: list[DiscoveredUrl] = []
        include_patterns = [item.lower() for item in policy.include_patterns]
        exclude_patterns = [item.lower() for item in policy.exclude_patterns]
        include_authors = [item.lower() for item in policy.include_authors]
        exclude_authors = [item.lower() for item in policy.exclude_authors]
        include_categories = [item.lower() for item in policy.include_categories]
        exclude_categories = [item.lower() for item in policy.exclude_categories]
        max_age_days = policy.max_age_days
        for entry in entries:
            haystack = " ".join(
                part.strip()
                for part in (entry.title or "", entry.url or "")
                if part and part.strip()
            ).lower()
            author_haystack = " ".join(
                (entry.author or "").strip().split()
            ).lower()
            categories = [
                " ".join((item or "").strip().split()).lower()
                for item in (entry.categories or [])
                if (item or "").strip()
            ]
            domain = (urlparse(entry.url).hostname or "").strip().lower().strip(".")
            if include_patterns and not any(
                pattern in haystack for pattern in include_patterns
            ):
                continue
            if exclude_patterns and any(
                pattern in haystack for pattern in exclude_patterns
            ):
                continue
            if policy.allowed_domains and not IngestScheduler._domain_matches(
                domain,
                policy.allowed_domains,
            ):
                continue
            if policy.blocked_domains and IngestScheduler._domain_matches(
                domain,
                policy.blocked_domains,
            ):
                continue
            if include_authors and not any(
                pattern in author_haystack for pattern in include_authors
            ):
                continue
            if exclude_authors and any(
                pattern in author_haystack for pattern in exclude_authors
            ):
                continue
            if include_categories and not IngestScheduler._categories_match(
                categories,
                include_categories,
            ):
                continue
            if exclude_categories and IngestScheduler._categories_match(
                categories,
                exclude_categories,
            ):
                continue
            if max_age_days > 0 and IngestScheduler._entry_is_older_than(
                entry.published_at,
                max_age_days=max_age_days,
            ):
                continue
            filtered.append(entry)
        return filtered

    @staticmethod
    def _domain_matches(domain: str, rules: list[str]) -> bool:
        normalized_domain = (domain or "").strip().lower().strip(".")
        if not normalized_domain:
            return False
        for rule in rules:
            normalized_rule = (rule or "").strip().lower().strip(".")
            if not normalized_rule:
                continue
            if (
                normalized_domain == normalized_rule
                or normalized_domain.endswith(f".{normalized_rule}")
            ):
                return True
        return False

    @staticmethod
    def _categories_match(categories: list[str], patterns: list[str]) -> bool:
        if not categories:
            return False
        return any(
            pattern in category
            for pattern in patterns
            for category in categories
        )

    @staticmethod
    def _pattern_match_count(haystack: str, patterns: list[str]) -> int:
        normalized_haystack = (haystack or "").strip().lower()
        if not normalized_haystack:
            return 0
        return sum(1 for pattern in patterns if pattern and pattern in normalized_haystack)

    @staticmethod
    def _domain_match_count(domain: str, rules: list[str]) -> int:
        normalized_domain = (domain or "").strip().lower().strip(".")
        if not normalized_domain:
            return 0
        return sum(
            1
            for rule in rules
            if rule
            and (
                normalized_domain == rule
                or normalized_domain.endswith(f".{rule}")
            )
        )

    @staticmethod
    def _categories_match_count(categories: list[str], patterns: list[str]) -> int:
        if not categories:
            return 0
        return sum(
            1
            for pattern in patterns
            if pattern and any(pattern in category for category in categories)
        )

    @staticmethod
    def _feed_entry_priority_key(
        entry: DiscoveredUrl,
        *,
        policy,
        index: int,
    ) -> tuple[int, int, int, int, float, int]:
        haystack = " ".join(
            part.strip()
            for part in (entry.title or "", entry.url or "")
            if part and part.strip()
        ).lower()
        author_haystack = " ".join((entry.author or "").strip().split()).lower()
        categories = [
            " ".join((item or "").strip().split()).lower()
            for item in (entry.categories or [])
            if (item or "").strip()
        ]
        domain = (urlparse(entry.url).hostname or "").strip().lower().strip(".")
        return (
            IngestScheduler._pattern_match_count(haystack, policy.priority_patterns),
            IngestScheduler._domain_match_count(domain, policy.preferred_domains),
            IngestScheduler._pattern_match_count(
                author_haystack,
                policy.preferred_authors,
            ),
            IngestScheduler._categories_match_count(
                categories,
                policy.preferred_categories,
            ),
            IngestScheduler._published_sort_key(entry.published_at),
            -index,
        )

    @staticmethod
    def _merge_recent_item_keys(
        current_item_keys: list[str],
        previous_item_keys: list[str],
    ) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for item in [*current_item_keys, *previous_item_keys]:
            normalized = (item or "").strip()
            if not normalized or normalized in seen:
                continue
            merged.append(normalized)
            seen.add(normalized)
        return merged

    @staticmethod
    def _merge_item_published_at(
        previous_item_published_at: dict[str, str],
        current_item_published_at: dict[str, str],
    ) -> dict[str, str]:
        merged = {
            str(key): str(value).strip()
            for key, value in previous_item_published_at.items()
            if str(key).strip() and str(value).strip()
        }
        for key, value in current_item_published_at.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if not normalized_key or not normalized_value:
                continue
            merged[normalized_key] = normalized_value
        return merged

    @staticmethod
    def _apply_feed_retention(
        *,
        source: MonitoredSource,
        hashes: dict[str, str],
        recent_item_keys: list[str],
        item_published_at: dict[str, str],
    ) -> tuple[dict[str, str], list[str], dict[str, str]]:
        policy = source.feed_retention
        if source.resolved_source_type() != "feed":
            return hashes, recent_item_keys, item_published_at

        ordering = IngestScheduler._order_feed_item_keys(
            recent_item_keys,
            hashes=hashes,
            item_published_at=item_published_at,
        )
        if policy.max_age_days > 0:
            ordering = [
                key
                for key in ordering
                if not IngestScheduler._entry_is_older_than(
                    item_published_at.get(key),
                    max_age_days=policy.max_age_days,
                )
            ]
        if policy.mode == "latest":
            ordering = ordering[: policy.max_items]

        retained_keys = ordering
        retained_set = set(retained_keys)
        retained_hashes = {
            key: value for key, value in hashes.items() if key in retained_set
        }
        retained_item_published_at = {
            key: value for key, value in item_published_at.items() if key in retained_set
        }
        return retained_hashes, retained_keys, retained_item_published_at

    @staticmethod
    def _order_feed_item_keys(
        recent_item_keys: list[str],
        *,
        hashes: dict[str, str],
        item_published_at: dict[str, str],
    ) -> list[str]:
        ordering_source = recent_item_keys or list(hashes.keys())
        ordered_keys: list[str] = []
        seen: set[str] = set()
        for item in ordering_source:
            normalized = (item or "").strip()
            if not normalized or normalized in seen:
                continue
            ordered_keys.append(normalized)
            seen.add(normalized)

        index_map = {key: index for index, key in enumerate(ordered_keys)}
        return sorted(
            ordered_keys,
            key=lambda key: (
                IngestScheduler._published_sort_key(item_published_at.get(key)),
                -index_map.get(key, 0),
            ),
            reverse=True,
        )

    @staticmethod
    def _published_sort_key(value: str | None) -> float:
        parsed = _parse_iso8601(value)
        if parsed is None:
            return float("-inf")
        return parsed.timestamp()

    @staticmethod
    def _entry_is_older_than(
        published_at: str | None,
        *,
        max_age_days: float,
    ) -> bool:
        if max_age_days <= 0:
            return False
        parsed = _parse_iso8601(published_at)
        if parsed is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        return parsed < cutoff

    @staticmethod
    def _build_poll_summary(
        *,
        source: MonitoredSource,
        requested_count: int,
        success_count: int,
        ingested_count: int,
        feed_discovered_count: int,
        feed_filtered_count: int,
    ) -> str:
        if source.resolved_source_type() != "feed":
            return (
                f"Polled {requested_count} page(s), {success_count} succeeded, "
                f"{ingested_count} ingested"
            )
        return (
            f"Discovered {feed_discovered_count} feed item(s), filtered {feed_filtered_count}, "
            f"crawled {requested_count}, {success_count} succeeded, {ingested_count} ingested"
        )

    @staticmethod
    def _effective_interval_seconds(
        source: MonitoredSource,
        record: CrawlStateRecord | None,
    ) -> int:
        base_interval = max(1, source.interval_seconds)
        failure_interval = IngestScheduler._apply_failure_backoff(
            base_interval=base_interval,
            record=record,
        )
        if record is None or source.resolved_schedule_mode() != "adaptive_feed":
            return failure_interval
        if record.consecutive_no_change <= 0:
            return failure_interval
        no_change_multiplier = min(
            MAX_FEED_NO_CHANGE_MULTIPLIER,
            1 + record.consecutive_no_change,
        )
        adaptive_interval = base_interval * no_change_multiplier
        return max(failure_interval, adaptive_interval)

    @staticmethod
    def _apply_failure_backoff(
        *,
        base_interval: int,
        record: CrawlStateRecord | None,
    ) -> int:
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
