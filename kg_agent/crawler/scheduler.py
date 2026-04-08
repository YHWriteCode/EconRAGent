from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import socket
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from lightrag_fork.kg.redis_lock_backend import RedisLockBackend
from lightrag_fork.utils import compute_mdhash_id

from kg_agent.config import SchedulerConfig
from kg_agent.crawler.content_extractor import canonicalize_url
from kg_agent.crawler.crawl_state_store import (
    CrawlStateRecord,
    CrawlStateStore,
    EventClusterRecord,
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
FEED_FINGERPRINT_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9]{2,}")
URL_TEXT_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
EVENT_TITLE_TOKEN_LIMIT = 18
EVENT_SIGNATURE_TOKEN_LIMIT = 72
GLOBAL_EVENT_VECTOR_TOP_K = 12


@dataclass
class EventClusterMatch:
    cluster_id: str
    owner_source_id: str
    similarity: float
    adjudicated_by_llm: bool = False


@dataclass
class WorkspaceEventClusterCandidate:
    owner_source_id: str
    source: MonitoredSource
    record: CrawlStateRecord
    cluster_record: EventClusterRecord


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
        utility_llm_client: Any | None = None,
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
        self.utility_llm_client = utility_llm_client
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
        existing_record = await self.state_store.get_record(stored.source_id)
        if existing_record is None or existing_record.last_status == "removed":
            await self.state_store.put_record(CrawlStateRecord(source_id=stored.source_id))
        return stored

    async def remove_source(self, source_id: str) -> bool:
        source = await self.source_registry.get_source(source_id)
        record = await self.state_store.get_record(source_id)
        tombstone_record: CrawlStateRecord | None = None
        if source is not None and record is not None:
            tombstone_record = await self._prepare_removed_source_tombstone(
                source=source,
                record=record,
            )
        removed = await self.source_registry.remove_source(source_id)
        if removed:
            if tombstone_record is None:
                await self.state_store.remove_record(source_id)
            else:
                await self.state_store.put_record(tombstone_record)
            self._source_locks.pop(source_id, None)
        return removed

    async def trigger_now(self, source_id: str) -> dict[str, Any]:
        source = await self.source_registry.get_source(source_id)
        if source is None:
            return {"status": "not_found", "source_id": source_id}
        return await self._poll_source(source)

    def utility_llm_available(self) -> bool:
        return self._utility_llm_available()

    async def get_status(self) -> dict[str, Any]:
        sources = await self.source_registry.list_sources()
        source_items: list[dict[str, Any]] = []
        utility_available = self.utility_llm_available()
        for source in sources:
            record = await self.state_store.get_record(source.source_id)
            effective_interval_seconds = self._effective_interval_seconds(source, record)
            source_items.append(
                {
                    **source.to_public_dict(utility_available=utility_available),
                    "last_crawled_at": None if record is None else record.last_crawled_at,
                    "last_status": "never_run" if record is None else record.last_status,
                    "consecutive_failures": 0 if record is None else record.consecutive_failures,
                    "consecutive_no_change": 0
                    if record is None
                    else record.consecutive_no_change,
                    "tracked_item_count": 0
                    if record is None
                    else len(record.last_content_hashes),
                    "active_doc_count": 0
                    if record is None
                    else self._count_active_doc_ids(record),
                    "expired_doc_count": 0
                    if record is None
                    else self._count_expired_doc_ids(record.doc_expires_at),
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
            await self._sweep_expired_documents()
            return

        if not await self._ensure_loop_leader_lease():
            self._leader_role = "standby"
            return

        self._leader_role = "leader"
        await self._poll_due_sources()
        await self._sweep_expired_documents()

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
            resolved_source_type = source.resolved_source_type()
            short_term_lifecycle = source.content_lifecycle.tracks_short_term_lifecycle(
                source_type=resolved_source_type
            )
            resolved_update_mode = source.content_lifecycle.resolved_update_mode(
                source_type=resolved_source_type
            )
            previous = await self.state_store.get_record(source.source_id) or CrawlStateRecord(
                source_id=source.source_id
            )
            raw_previous_recent_item_keys = previous.recent_item_keys
            previous_recent_item_keys = previous.recent_item_keys
            previous_item_published_at = previous.item_published_at
            previous_item_content_fingerprints = previous.item_content_fingerprints
            previous_item_active_doc_ids = previous.item_active_doc_ids
            previous_item_event_cluster_ids = previous.item_event_cluster_ids
            previous_event_clusters = previous.event_clusters
            previous_doc_expires_at = previous.doc_expires_at
            hashes = dict(previous.last_content_hashes)
            if resolved_source_type == "feed":
                previous_recent_item_keys = self._canonicalize_feed_item_keys(
                    raw_previous_recent_item_keys
                )
                previous_item_published_at = self._canonicalize_feed_url_map(
                    previous.item_published_at,
                    preferred_order=raw_previous_recent_item_keys,
                )
                previous_item_content_fingerprints = self._canonicalize_feed_url_map(
                    previous.item_content_fingerprints,
                    preferred_order=raw_previous_recent_item_keys,
                )
                hashes = self._canonicalize_feed_url_map(
                    previous.last_content_hashes,
                    preferred_order=raw_previous_recent_item_keys,
                )
                previous_item_active_doc_ids = self._canonicalize_feed_url_map(
                    previous.item_active_doc_ids,
                    preferred_order=raw_previous_recent_item_keys,
                )
                previous_item_event_cluster_ids = self._canonicalize_feed_url_map(
                    previous.item_event_cluster_ids,
                    preferred_order=raw_previous_recent_item_keys,
                )
                previous_event_clusters = self._canonicalize_event_clusters(
                    previous.event_clusters,
                    item_event_cluster_ids=previous_item_event_cluster_ids,
                )
            if not short_term_lifecycle:
                previous_item_active_doc_ids = {}
                previous_item_event_cluster_ids = {}
                previous_event_clusters = {}
                previous_doc_expires_at = {}
            active_doc_ids = dict(previous_item_active_doc_ids)
            item_event_cluster_ids = dict(previous_item_event_cluster_ids)
            event_clusters = {
                cluster_id: EventClusterRecord.from_dict(
                    cluster_id,
                    cluster_record.to_dict()
                    if isinstance(cluster_record, EventClusterRecord)
                    else cluster_record,
                )
                for cluster_id, cluster_record in previous_event_clusters.items()
            }
            doc_expires_at = dict(previous_doc_expires_at)
            requested_count = 0
            ingested_count = 0
            success_count = 0
            failure_messages: list[str] = []
            current_feed_item_published_at: dict[str, str] = {}
            current_feed_item_content_fingerprints: dict[str, str] = {}
            retained_feed_item_keys: list[str] = []
            feed_discovered_count = 0
            feed_filtered_count = 0
            feed_deduplicated_count = 0
            superseded_count = 0
            expired_doc_count = 0
            deleted_doc_count = 0
            event_cluster_count = len(event_clusters)
            rag = None
            event_cluster_mode = source.content_lifecycle.resolved_event_cluster_mode(
                source_type=resolved_source_type,
                utility_available=self._utility_llm_available(),
            )
            workspace_clusters: dict[str, WorkspaceEventClusterCandidate] = {}
            workspace_records: dict[str, CrawlStateRecord] = {}
            workspace_sources: dict[str, MonitoredSource] = {}
            pending_workspace_records: dict[str, CrawlStateRecord] = {}
            if (
                resolved_source_type == "feed"
                and short_term_lifecycle
                and source.content_lifecycle.event_clustering_enabled(
                    source_type=resolved_source_type,
                    utility_available=self._utility_llm_available(),
                )
            ):
                (
                    workspace_clusters,
                    workspace_records,
                    workspace_sources,
                ) = await self._collect_workspace_event_cluster_candidates(source=source)

            try:
                pages, crawl_metadata = await self._collect_pages_for_source(source)
                requested_count = len(pages)
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
                fingerprint_owner_by_value = self._build_fingerprint_owner_index(
                    previous_recent_item_keys,
                    item_content_fingerprints=previous_item_content_fingerprints,
                )
                duplicate_published_at_updates: dict[str, str] = {}
                for page in pages:
                    page_url = (page.final_url or page.url or "").strip() or page.url
                    page_key = page_url
                    published_at: str | None = None
                    if resolved_source_type == "feed":
                        requested_page_key = self._canonicalize_feed_item_key(page.url)
                        page_key = self._canonicalize_feed_item_key(page_url)
                        published_at = (
                            current_feed_item_published_at.get(page_key)
                            or current_feed_item_published_at.get(requested_page_key)
                        )
                        if published_at:
                            current_feed_item_published_at[page_key] = published_at
                    if not page.success:
                        failure_messages.append(
                            f"{page_key}: {page.error or 'crawl_failed'}"
                        )
                        continue

                    success_count += 1
                    content = (page.markdown or "").strip()
                    if not content:
                        continue

                    content_fingerprint = ""
                    if resolved_source_type == "feed":
                        content_fingerprint = self._build_feed_content_fingerprint(
                            title=page.title,
                            content=content,
                            source=source,
                        )
                        duplicate_owner = (
                            fingerprint_owner_by_value.get(content_fingerprint)
                            if content_fingerprint and source.feed_dedup.is_active()
                            else None
                        )
                        if duplicate_owner and duplicate_owner != page_key:
                            feed_deduplicated_count += 1
                            published_at = current_feed_item_published_at.get(page_key)
                            if published_at:
                                duplicate_published_at_updates[
                                    duplicate_owner
                                ] = self._select_newer_timestamp(
                                    duplicate_published_at_updates.get(duplicate_owner),
                                    published_at,
                                )
                            continue
                        retained_feed_item_keys.append(page_key)
                        if content_fingerprint:
                            current_feed_item_content_fingerprints[page_key] = (
                                content_fingerprint
                            )
                            fingerprint_owner_by_value.setdefault(
                                content_fingerprint,
                                page_key,
                            )

                    content_hash = compute_mdhash_id(content)
                    old_hash = hashes.get(page_key)
                    hashes[page_key] = content_hash
                    event_cluster_match = None
                    if (
                        resolved_source_type == "feed"
                        and short_term_lifecycle
                        and source.content_lifecycle.event_clustering_enabled(
                            source_type=resolved_source_type,
                            utility_available=self._utility_llm_available(),
                        )
                    ):
                        if rag is None and workspace_clusters:
                            rag = await self._resolve_rag(source.workspace)
                        event_cluster_match = await self._match_event_cluster(
                            source=source,
                            page_key=page_key,
                            title=page.title,
                            content=content,
                            published_at=published_at,
                            event_clusters=event_clusters,
                            mode=event_cluster_mode,
                            rag=rag,
                            workspace_clusters=workspace_clusters,
                        )
                        if event_cluster_match is None:
                            item_event_cluster_ids[page_key] = self._create_event_cluster_id(
                                source=source,
                                page_key=page_key,
                            )
                        else:
                            item_event_cluster_ids[page_key] = event_cluster_match.cluster_id
                    if old_hash == content_hash:
                        if (
                            event_cluster_match is None
                            and resolved_source_type == "feed"
                            and short_term_lifecycle
                            and page_key in item_event_cluster_ids
                        ):
                            event_clusters = self._update_event_cluster_record(
                                event_clusters=event_clusters,
                                cluster_id=item_event_cluster_ids[page_key],
                                page_key=page_key,
                                title=page.title,
                                content=content,
                                content_fingerprint=content_fingerprint,
                                published_at=published_at,
                                active_doc_id=(
                                    event_clusters.get(item_event_cluster_ids[page_key]).active_doc_id
                                    if event_clusters.get(item_event_cluster_ids[page_key])
                                    is not None
                                    else active_doc_ids.get(page_key)
                                ),
                                similarity=1.0,
                                adjudicated_by_llm=False,
                                updated_at=now_iso,
                            )
                        continue

                    if rag is None:
                        rag = await self._resolve_rag(source.workspace)
                    doc_id = self._build_scheduler_doc_id(
                        source=source,
                        page_key=page_key,
                        content_hash=content_hash,
                    )
                    inserted_doc_id = await self._insert_scheduler_document(
                        rag=rag,
                        content=content,
                        file_path=page_key
                        if resolved_source_type == "feed"
                        else (page.final_url or page.url),
                        doc_id=doc_id,
                    )
                    ingested_count += 1
                    if short_term_lifecycle:
                        previous_active_doc_id = active_doc_ids.get(page_key)
                        cluster_id = item_event_cluster_ids.get(page_key)
                        cluster_owner_source_id = source.source_id
                        if event_cluster_match is not None:
                            cluster_owner_source_id = event_cluster_match.owner_source_id
                        if cluster_id:
                            previous_active_doc_id = (
                                self._get_cluster_active_doc_id(
                                    cluster_id=cluster_id,
                                    event_clusters=event_clusters,
                                    workspace_clusters=workspace_clusters,
                                )
                                or previous_active_doc_id
                            )
                        if (
                            resolved_update_mode == "replace_latest"
                            and previous_active_doc_id
                            and previous_active_doc_id != inserted_doc_id
                        ):
                            if cluster_owner_source_id == source.source_id:
                                doc_expires_at[previous_active_doc_id] = now_iso
                            else:
                                owner_record = workspace_records.get(cluster_owner_source_id)
                                if owner_record is not None:
                                    updated_doc_expires_at = dict(owner_record.doc_expires_at)
                                    updated_doc_expires_at[previous_active_doc_id] = now_iso
                                    owner_record = replace(
                                        owner_record,
                                        doc_expires_at=updated_doc_expires_at,
                                    )
                                    workspace_records[cluster_owner_source_id] = owner_record
                                    pending_workspace_records[
                                        cluster_owner_source_id
                                    ] = owner_record
                            superseded_count += 1
                        active_doc_ids[page_key] = inserted_doc_id
                        active_expiry = self._build_active_doc_expiry(
                            source=source,
                            source_type=resolved_source_type,
                            published_at=published_at,
                            now_iso=now_iso,
                        )
                        if active_expiry:
                            doc_expires_at[inserted_doc_id] = active_expiry
                        else:
                            doc_expires_at.pop(inserted_doc_id, None)
                        if cluster_id:
                            updated_clusters = self._update_event_cluster_record(
                                event_clusters=(
                                    event_clusters
                                    if cluster_owner_source_id == source.source_id
                                    else (
                                        workspace_records.get(
                                            cluster_owner_source_id,
                                            CrawlStateRecord(
                                                source_id=cluster_owner_source_id
                                            ),
                                        ).event_clusters
                                    )
                                ),
                                cluster_id=cluster_id,
                                page_key=page_key,
                                title=page.title,
                                content=content,
                                content_fingerprint=content_fingerprint,
                                published_at=published_at,
                                active_doc_id=inserted_doc_id,
                                similarity=(
                                    1.0
                                    if event_cluster_match is None
                                    else event_cluster_match.similarity
                                ),
                                adjudicated_by_llm=bool(
                                    event_cluster_match
                                    and event_cluster_match.adjudicated_by_llm
                                ),
                                updated_at=now_iso,
                            )
                            if cluster_owner_source_id == source.source_id:
                                event_clusters = updated_clusters
                            else:
                                owner_record = workspace_records.get(cluster_owner_source_id)
                                if owner_record is not None:
                                    owner_record = replace(
                                        owner_record,
                                        event_clusters=updated_clusters,
                                    )
                                    workspace_records[cluster_owner_source_id] = owner_record
                                    pending_workspace_records[
                                        cluster_owner_source_id
                                    ] = owner_record
                                    workspace_clusters[cluster_id] = (
                                        WorkspaceEventClusterCandidate(
                                            owner_source_id=cluster_owner_source_id,
                                            source=workspace_sources.get(
                                                cluster_owner_source_id,
                                                source,
                                            ),
                                            record=owner_record,
                                            cluster_record=updated_clusters[cluster_id],
                                        )
                                    )

                recent_item_keys = previous_recent_item_keys
                item_published_at = previous_item_published_at
                item_content_fingerprints = previous_item_content_fingerprints
                if resolved_source_type == "feed":
                    recent_item_keys = self._merge_recent_item_keys(
                        retained_feed_item_keys,
                        previous_recent_item_keys,
                    )
                    item_published_at = self._merge_item_published_at(
                        previous_item_published_at,
                        current_feed_item_published_at,
                    )
                    item_published_at = self._merge_item_published_at(
                        item_published_at,
                        duplicate_published_at_updates,
                    )
                    item_content_fingerprints = self._merge_item_content_fingerprints(
                        previous_item_content_fingerprints,
                        current_feed_item_content_fingerprints,
                    )
                    (
                        hashes,
                        recent_item_keys,
                        item_published_at,
                        item_content_fingerprints,
                    ) = self._apply_feed_retention(
                        source=source,
                        hashes=hashes,
                        recent_item_keys=recent_item_keys,
                        item_published_at=item_published_at,
                        item_content_fingerprints=item_content_fingerprints,
                    )
                    pre_retention_item_event_cluster_ids = dict(item_event_cluster_ids)
                    (
                        item_event_cluster_ids,
                        event_clusters,
                        retired_cluster_doc_ids,
                    ) = self._apply_event_cluster_retention(
                        item_event_cluster_ids=item_event_cluster_ids,
                        event_clusters=event_clusters,
                        retained_item_keys=recent_item_keys,
                    )
                else:
                    pre_retention_item_event_cluster_ids = dict(item_event_cluster_ids)
                    retired_cluster_doc_ids = []
                if short_term_lifecycle and resolved_source_type == "feed":
                    retained_keys = set(recent_item_keys)
                    retired_item_keys = [
                        key for key in list(active_doc_ids.keys()) if key not in retained_keys
                    ]
                    for retired_key in retired_item_keys:
                        retired_cluster_id = pre_retention_item_event_cluster_ids.get(
                            retired_key
                        )
                        retired_doc_id = active_doc_ids.pop(retired_key, None)
                        if (
                            retired_doc_id
                            and retired_cluster_id
                            and self._get_cluster_active_doc_id(
                                cluster_id=retired_cluster_id,
                                event_clusters=event_clusters,
                                workspace_clusters=workspace_clusters,
                            )
                            == retired_doc_id
                        ):
                            continue
                        if retired_doc_id:
                            doc_expires_at[retired_doc_id] = now_iso
                            superseded_count += 1
                    for retired_cluster_doc_id in retired_cluster_doc_ids:
                        if retired_cluster_doc_id:
                            doc_expires_at[retired_cluster_doc_id] = now_iso
                            superseded_count += 1
                if short_term_lifecycle:
                    expired_doc_ids = self._collect_expired_doc_ids(
                        doc_expires_at,
                        now_iso=now_iso,
                    )
                    expired_doc_count = len(expired_doc_ids)
                    expired_doc_id_set = set(expired_doc_ids)
                    if expired_doc_id_set:
                        active_doc_ids = {
                            key: value
                            for key, value in active_doc_ids.items()
                            if value not in expired_doc_id_set
                        }
                        event_clusters = self._clear_event_cluster_active_doc_ids(
                            event_clusters=event_clusters,
                            doc_ids=expired_doc_id_set,
                        )
                        if workspace_sources:
                            pending_workspace_records.update(
                                self._clear_workspace_cluster_active_doc_ids(
                                    doc_ids=expired_doc_id_set,
                                    workspace_records=workspace_records,
                                    workspace_sources=workspace_sources,
                                    skip_source_id=source.source_id,
                                )
                            )
                    if expired_doc_ids and source.content_lifecycle.delete_expired:
                        if rag is None:
                            rag = await self._resolve_rag(source.workspace)
                        deleted_doc_ids = await self._delete_documents_by_id(
                            rag=rag,
                            doc_ids=expired_doc_ids,
                        )
                        deleted_doc_count = len(deleted_doc_ids)
                        deleted_doc_id_set = set(deleted_doc_ids)
                        for doc_id in deleted_doc_ids:
                            doc_expires_at.pop(doc_id, None)
                        active_doc_ids = {
                            key: value
                            for key, value in active_doc_ids.items()
                            if value not in deleted_doc_id_set
                        }
                        event_clusters = self._clear_event_cluster_active_doc_ids(
                            event_clusters=event_clusters,
                            doc_ids=deleted_doc_id_set,
                        )
                        if deleted_doc_id_set and workspace_sources:
                            pending_workspace_records.update(
                                self._clear_workspace_cluster_active_doc_ids(
                                    doc_ids=deleted_doc_id_set,
                                    workspace_records=workspace_records,
                                    workspace_sources=workspace_sources,
                                    skip_source_id=source.source_id,
                                )
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
                    item_content_fingerprints=item_content_fingerprints,
                    item_active_doc_ids=active_doc_ids,
                    item_event_cluster_ids=item_event_cluster_ids,
                    event_clusters=event_clusters,
                    doc_expires_at=doc_expires_at,
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
                for owner_source_id, owner_record in pending_workspace_records.items():
                    if owner_source_id == source.source_id:
                        continue
                    await self.state_store.put_record(owner_record)
                return {
                    "status": last_status,
                    "source_id": source.source_id,
                    "requested_count": requested_count,
                    "success_count": success_count,
                    "ingested_count": ingested_count,
                    "feed_discovered_count": feed_discovered_count,
                    "feed_filtered_count": feed_filtered_count,
                    "feed_deduplicated_count": feed_deduplicated_count,
                    "superseded_count": superseded_count,
                    "expired_doc_count": expired_doc_count,
                    "deleted_doc_count": deleted_doc_count,
                    "tracked_item_count": len(hashes),
                    "summary": self._build_poll_summary(
                        source=source,
                        requested_count=requested_count,
                        success_count=success_count,
                        ingested_count=ingested_count,
                        feed_discovered_count=feed_discovered_count,
                        feed_filtered_count=feed_filtered_count,
                        feed_deduplicated_count=feed_deduplicated_count,
                        superseded_count=superseded_count,
                        deleted_doc_count=deleted_doc_count,
                    ),
                }
            except Exception as exc:  # pragma: no cover
                logger.exception("Polling source '%s' failed: %s", source.source_id, exc)
                failure_messages.append(str(exc))
                record = CrawlStateRecord(
                    source_id=source.source_id,
                    last_crawled_at=now_iso,
                    last_content_hashes=hashes,
                    recent_item_keys=previous_recent_item_keys,
                    item_published_at=previous_item_published_at,
                    item_content_fingerprints=previous_item_content_fingerprints,
                    item_active_doc_ids=previous_item_active_doc_ids,
                    item_event_cluster_ids=previous_item_event_cluster_ids,
                    event_clusters=previous_event_clusters,
                    doc_expires_at=previous_doc_expires_at,
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
                    "feed_deduplicated_count": feed_deduplicated_count,
                    "superseded_count": superseded_count,
                    "expired_doc_count": expired_doc_count,
                    "deleted_doc_count": deleted_doc_count,
                    "tracked_item_count": len(hashes),
                    "summary": str(exc),
                }
            finally:
                await self.coordinator.release(coordination_lease)

    async def _sweep_expired_documents(self) -> None:
        sources = await self.source_registry.list_sources()
        for source in sources:
            resolved_source_type = source.resolved_source_type()
            if not source.content_lifecycle.delete_expired:
                continue
            if not source.content_lifecycle.tracks_short_term_lifecycle(
                source_type=resolved_source_type
            ):
                continue
            try:
                await self._cleanup_expired_documents_for_source(source)
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "Expired crawler document sweep failed for source '%s': %s",
                    source.source_id,
                    exc,
                )

    async def _cleanup_expired_documents_for_source(
        self,
        source: MonitoredSource,
    ) -> int:
        coordination_lease = await self.coordinator.acquire(
            f"source:{source.source_id}",
            ttl_s=self.coordination_ttl_seconds,
            wait_timeout_s=0.0,
        )
        if coordination_lease is None:
            return 0

        lock = self._source_locks.setdefault(source.source_id, asyncio.Lock())
        if lock.locked():
            await self.coordinator.release(coordination_lease)
            return 0

        async with lock:
            try:
                record = await self.state_store.get_record(source.source_id)
                if record is None or not record.doc_expires_at:
                    return 0

                now_iso = _utcnow_iso()
                expired_doc_ids = self._collect_expired_doc_ids(
                    record.doc_expires_at,
                    now_iso=now_iso,
                )
                if not expired_doc_ids:
                    return 0

                rag = await self._resolve_rag(source.workspace)
                deleted_doc_ids = await self._delete_documents_by_id(
                    rag=rag,
                    doc_ids=expired_doc_ids,
                )
                if not deleted_doc_ids:
                    return 0

                deleted_doc_id_set = set(deleted_doc_ids)
                updated_event_clusters = self._clear_event_cluster_active_doc_ids(
                    event_clusters=record.event_clusters,
                    doc_ids=deleted_doc_id_set,
                )
                await self.state_store.put_record(
                    CrawlStateRecord(
                        source_id=record.source_id,
                        last_crawled_at=record.last_crawled_at,
                        last_content_hashes=dict(record.last_content_hashes),
                        recent_item_keys=list(record.recent_item_keys),
                        item_published_at=dict(record.item_published_at),
                        item_content_fingerprints=dict(
                            record.item_content_fingerprints
                        ),
                        item_active_doc_ids={
                            key: value
                            for key, value in record.item_active_doc_ids.items()
                            if value not in deleted_doc_id_set
                        },
                        item_event_cluster_ids=dict(record.item_event_cluster_ids),
                        event_clusters=updated_event_clusters,
                        doc_expires_at={
                            key: value
                            for key, value in record.doc_expires_at.items()
                            if key not in deleted_doc_id_set
                        },
                        last_status=record.last_status,
                        consecutive_failures=record.consecutive_failures,
                        consecutive_no_change=record.consecutive_no_change,
                        total_ingested_count=record.total_ingested_count,
                        last_error=record.last_error,
                    )
                )
                (
                    _workspace_clusters,
                    workspace_records,
                    workspace_sources,
                ) = await self._collect_workspace_event_cluster_candidates(source=source)
                for owner_record in self._clear_workspace_cluster_active_doc_ids(
                    doc_ids=deleted_doc_id_set,
                    workspace_records=workspace_records,
                    workspace_sources=workspace_sources,
                    skip_source_id=source.source_id,
                ).values():
                    await self.state_store.put_record(owner_record)
                return len(deleted_doc_ids)
            finally:
                await self.coordinator.release(coordination_lease)

    async def _prepare_removed_source_tombstone(
        self,
        *,
        source: MonitoredSource,
        record: CrawlStateRecord,
    ) -> CrawlStateRecord | None:
        resolved_source_type = source.resolved_source_type()
        if not source.content_lifecycle.tracks_short_term_lifecycle(
            source_type=resolved_source_type
        ):
            return None

        managed_doc_ids = self._collect_managed_doc_ids(record)
        if not managed_doc_ids:
            return None

        now_iso = _utcnow_iso()
        rag = await self._resolve_rag(source.workspace)
        deleted_doc_ids = await self._delete_documents_by_id(
            rag=rag,
            doc_ids=managed_doc_ids,
        )
        deleted_doc_id_set = set(deleted_doc_ids)
        remaining_doc_ids = [
            doc_id for doc_id in managed_doc_ids if doc_id not in deleted_doc_id_set
        ]
        suppressed_doc_ids = {*(deleted_doc_id_set), *remaining_doc_ids}
        if suppressed_doc_ids:
            (
                _workspace_clusters,
                workspace_records,
                workspace_sources,
            ) = await self._collect_workspace_event_cluster_candidates(source=source)
            for owner_record in self._clear_workspace_cluster_active_doc_ids(
                doc_ids=suppressed_doc_ids,
                workspace_records=workspace_records,
                workspace_sources=workspace_sources,
                skip_source_id=source.source_id,
            ).values():
                await self.state_store.put_record(owner_record)
        if not remaining_doc_ids:
            return None

        return CrawlStateRecord(
            source_id=record.source_id,
            last_crawled_at=record.last_crawled_at,
            last_content_hashes={},
            recent_item_keys=[],
            item_published_at={},
            item_content_fingerprints={},
            item_active_doc_ids={},
            item_event_cluster_ids={},
            event_clusters={},
            doc_expires_at={doc_id: now_iso for doc_id in remaining_doc_ids},
            last_status="removed",
            consecutive_failures=0,
            consecutive_no_change=0,
            total_ingested_count=record.total_ingested_count,
            last_error=record.last_error,
        )

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

    @staticmethod
    def _build_scheduler_doc_id(
        *,
        source: MonitoredSource,
        page_key: str,
        content_hash: str,
    ) -> str:
        return compute_mdhash_id(
            f"crawler:{source.source_id}:{page_key.strip()}:{content_hash.strip()}"
        )

    @staticmethod
    def _callable_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return False
        parameter = signature.parameters.get(keyword)
        if parameter is not None:
            return parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        return any(
            item.kind == inspect.Parameter.VAR_KEYWORD
            for item in signature.parameters.values()
        )

    async def _insert_scheduler_document(
        self,
        *,
        rag: Any,
        content: str,
        file_path: str,
        doc_id: str,
    ) -> str:
        kwargs: dict[str, Any] = {
            "input": content,
            "file_paths": file_path,
        }
        if self._callable_accepts_keyword(rag.ainsert, "ids"):
            kwargs["ids"] = doc_id
        await rag.ainsert(**kwargs)
        return doc_id

    async def _delete_documents_by_id(
        self,
        *,
        rag: Any,
        doc_ids: list[str],
    ) -> list[str]:
        delete_callable = getattr(rag, "adelete_by_doc_id", None)
        if delete_callable is None:
            logger.warning(
                "RAG provider for workspace '%s' does not support adelete_by_doc_id; skipping %d expired crawler document(s)",
                getattr(rag, "workspace", None),
                len(doc_ids),
            )
            return []
        deleted: list[str] = []
        for doc_id in doc_ids:
            try:
                await delete_callable(doc_id)
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to delete expired crawler document '%s': %s", doc_id, exc)
                continue
            deleted.append(doc_id)
        return deleted

    @staticmethod
    def _build_active_doc_expiry(
        *,
        source: MonitoredSource,
        source_type: str,
        published_at: str | None,
        now_iso: str,
    ) -> str | None:
        if not source.content_lifecycle.tracks_short_term_lifecycle(
            source_type=source_type
        ):
            return None
        if source.content_lifecycle.ttl_days <= 0:
            return None
        base_time = _parse_iso8601(published_at) or _parse_iso8601(now_iso)
        if base_time is None:
            return None
        return (base_time + timedelta(days=source.content_lifecycle.ttl_days)).isoformat()

    @staticmethod
    def _collect_expired_doc_ids(
        doc_expires_at: dict[str, str],
        *,
        now_iso: str,
    ) -> list[str]:
        now_dt = _parse_iso8601(now_iso)
        if now_dt is None:
            return []
        expired: list[str] = []
        for doc_id, expires_at in doc_expires_at.items():
            expires_dt = _parse_iso8601(expires_at)
            if expires_dt is None or expires_dt > now_dt:
                continue
            expired.append(str(doc_id).strip())
        return [doc_id for doc_id in expired if doc_id]

    @classmethod
    def _count_expired_doc_ids(cls, doc_expires_at: dict[str, str]) -> int:
        return len(cls._collect_expired_doc_ids(doc_expires_at, now_iso=_utcnow_iso()))

    @staticmethod
    def _collect_managed_doc_ids(record: CrawlStateRecord) -> list[str]:
        managed_doc_ids = [
            *record.item_active_doc_ids.values(),
            *[
                cluster_record.active_doc_id
                for cluster_record in record.event_clusters.values()
            ],
            *record.doc_expires_at.keys(),
        ]
        normalized: list[str] = []
        seen: set[str] = set()
        for doc_id in managed_doc_ids:
            item = str(doc_id or "").strip()
            if not item or item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        return normalized

    @staticmethod
    def _count_active_doc_ids(record: CrawlStateRecord) -> int:
        return len(
            {
                *[
                    str(value).strip()
                    for value in record.item_active_doc_ids.values()
                    if str(value).strip()
                ],
                *[
                    str(cluster_record.active_doc_id or "").strip()
                    for cluster_record in record.event_clusters.values()
                    if str(cluster_record.active_doc_id or "").strip()
                ],
            }
        )

    def _utility_llm_available(self) -> bool:
        return bool(
            self.utility_llm_client is not None
            and callable(getattr(self.utility_llm_client, "is_available", None))
            and self.utility_llm_client.is_available()
        )

    @staticmethod
    def _workspace_key(value: str | None) -> str:
        return (value or "").strip()

    async def _collect_workspace_event_cluster_candidates(
        self,
        *,
        source: MonitoredSource,
    ) -> tuple[
        dict[str, WorkspaceEventClusterCandidate],
        dict[str, CrawlStateRecord],
        dict[str, MonitoredSource],
    ]:
        workspace_sources: dict[str, MonitoredSource] = {}
        target_workspace = self._workspace_key(source.workspace)
        for candidate_source in await self.source_registry.list_sources():
            if self._workspace_key(candidate_source.workspace) != target_workspace:
                continue
            resolved_source_type = candidate_source.resolved_source_type()
            if not candidate_source.content_lifecycle.tracks_short_term_lifecycle(
                source_type=resolved_source_type
            ):
                continue
            workspace_sources[candidate_source.source_id] = candidate_source

        workspace_records = {
            record.source_id: record for record in await self.state_store.list_records()
        }
        now_iso = _utcnow_iso()
        workspace_clusters: dict[str, WorkspaceEventClusterCandidate] = {}
        for source_id, candidate_source in workspace_sources.items():
            record = workspace_records.get(source_id)
            if record is None:
                continue
            expired_doc_ids = set(
                self._collect_expired_doc_ids(record.doc_expires_at, now_iso=now_iso)
            )
            for cluster_id, cluster_record in record.event_clusters.items():
                active_doc_id = str(cluster_record.active_doc_id or "").strip()
                if not active_doc_id or active_doc_id in expired_doc_ids:
                    continue
                workspace_clusters[str(cluster_id).strip()] = (
                    WorkspaceEventClusterCandidate(
                        owner_source_id=source_id,
                        source=candidate_source,
                        record=record,
                        cluster_record=cluster_record,
                    )
                )
        return workspace_clusters, workspace_records, workspace_sources

    @staticmethod
    def _index_workspace_cluster_candidates_by_doc_id(
        workspace_clusters: dict[str, WorkspaceEventClusterCandidate],
    ) -> dict[str, WorkspaceEventClusterCandidate]:
        indexed: dict[str, WorkspaceEventClusterCandidate] = {}
        for candidate in workspace_clusters.values():
            doc_id = str(candidate.cluster_record.active_doc_id or "").strip()
            if doc_id and doc_id not in indexed:
                indexed[doc_id] = candidate
        return indexed

    async def _query_workspace_event_cluster_vector_candidates(
        self,
        *,
        rag: Any,
        event_signature: str,
        workspace_candidates_by_doc_id: dict[str, WorkspaceEventClusterCandidate],
    ) -> dict[str, float]:
        if not event_signature or not workspace_candidates_by_doc_id:
            return {}
        chunks_vdb = getattr(rag, "chunks_vdb", None)
        query = getattr(chunks_vdb, "query", None)
        if not callable(query):
            return {}

        try:
            results = await query(event_signature, top_k=GLOBAL_EVENT_VECTOR_TOP_K)
        except Exception as exc:  # pragma: no cover
            logger.warning("Workspace event-cluster vector query failed: %s", exc)
            return {}

        vector_scores: dict[str, float] = {}
        total_results = max(1, len(results))
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            doc_id = str(result.get("full_doc_id") or "").strip()
            if not doc_id:
                continue
            candidate = workspace_candidates_by_doc_id.get(doc_id)
            if candidate is None:
                continue
            try:
                raw_score = float(result.get("distance") or 0.0)
            except (TypeError, ValueError):
                raw_score = 0.0
            rank_score = 1.0 - (index / total_results)
            normalized_score = max(0.0, min(1.0, max(raw_score, rank_score)))
            cluster_id = candidate.cluster_record.cluster_id
            vector_scores[cluster_id] = max(
                vector_scores.get(cluster_id, 0.0),
                normalized_score,
            )
        return vector_scores

    @staticmethod
    def _combine_event_cluster_similarity(
        *,
        heuristic_similarity: float,
        vector_similarity: float,
    ) -> float:
        heuristic_score = max(0.0, min(1.0, float(heuristic_similarity or 0.0)))
        vector_score = max(0.0, min(1.0, float(vector_similarity or 0.0)))
        if vector_score <= 0:
            return heuristic_score
        return max(
            heuristic_score,
            (0.25 * heuristic_score) + (0.75 * vector_score),
        )

    @staticmethod
    def _get_cluster_active_doc_id(
        *,
        cluster_id: str | None,
        event_clusters: dict[str, EventClusterRecord],
        workspace_clusters: dict[str, WorkspaceEventClusterCandidate],
    ) -> str | None:
        normalized_cluster_id = str(cluster_id or "").strip()
        if not normalized_cluster_id:
            return None
        local_cluster = event_clusters.get(normalized_cluster_id)
        if local_cluster is not None:
            return str(local_cluster.active_doc_id or "").strip() or None
        candidate = workspace_clusters.get(normalized_cluster_id)
        if candidate is None:
            return None
        return str(candidate.cluster_record.active_doc_id or "").strip() or None

    @staticmethod
    def _clear_event_cluster_active_doc_ids(
        *,
        event_clusters: dict[str, EventClusterRecord],
        doc_ids: set[str],
    ) -> dict[str, EventClusterRecord]:
        if not doc_ids:
            return event_clusters
        updated_clusters: dict[str, EventClusterRecord] = {}
        for cluster_id, cluster_record in event_clusters.items():
            active_doc_id = str(cluster_record.active_doc_id or "").strip()
            if active_doc_id and active_doc_id in doc_ids:
                updated_clusters[cluster_id] = replace(
                    cluster_record,
                    active_doc_id=None,
                )
                continue
            updated_clusters[cluster_id] = cluster_record
        return updated_clusters

    def _clear_workspace_cluster_active_doc_ids(
        self,
        *,
        doc_ids: set[str],
        workspace_records: dict[str, CrawlStateRecord],
        workspace_sources: dict[str, MonitoredSource],
        skip_source_id: str | None = None,
    ) -> dict[str, CrawlStateRecord]:
        if not doc_ids:
            return {}
        updated_records: dict[str, CrawlStateRecord] = {}
        for source_id, candidate_source in workspace_sources.items():
            if source_id == skip_source_id:
                continue
            record = workspace_records.get(source_id)
            if record is None or not record.event_clusters:
                continue
            updated_clusters = self._clear_event_cluster_active_doc_ids(
                event_clusters=record.event_clusters,
                doc_ids=doc_ids,
            )
            if updated_clusters == record.event_clusters:
                continue
            updated_record = replace(
                record,
                event_clusters=updated_clusters,
            )
            workspace_records[source_id] = updated_record
            updated_records[source_id] = updated_record
        return updated_records

    async def _match_event_cluster(
        self,
        *,
        source: MonitoredSource,
        page_key: str,
        title: str | None,
        content: str,
        published_at: str | None,
        event_clusters: dict[str, EventClusterRecord],
        mode: str,
        rag: Any | None = None,
        workspace_clusters: dict[str, WorkspaceEventClusterCandidate] | None = None,
    ) -> EventClusterMatch | None:
        if not event_clusters:
            event_clusters = {}

        event_signature = self._build_event_signature_text(title=title, content=content)
        title_text = self._normalize_feed_fingerprint_text(title)
        if not event_signature and not title_text:
            return None

        candidate_scores: dict[
            str,
            dict[str, Any],
        ] = {}
        for cluster_record in event_clusters.values():
            heuristic_similarity = self._score_event_cluster_candidate(
                cluster_record=cluster_record,
                title_text=title_text,
                signature_text=event_signature,
                published_at=published_at,
                window_days=source.content_lifecycle.event_cluster_window_days,
            )
            if heuristic_similarity <= 0:
                continue
            candidate_scores[cluster_record.cluster_id] = {
                "owner_source_id": source.source_id,
                "cluster_record": cluster_record,
                "heuristic_similarity": heuristic_similarity,
                "vector_similarity": 0.0,
                "similarity": heuristic_similarity,
            }

        workspace_clusters = workspace_clusters or {}
        if rag is not None and workspace_clusters:
            vector_scores = await self._query_workspace_event_cluster_vector_candidates(
                rag=rag,
                event_signature=event_signature,
                workspace_candidates_by_doc_id=self._index_workspace_cluster_candidates_by_doc_id(
                    workspace_clusters
                ),
            )
            for cluster_id, vector_similarity in vector_scores.items():
                workspace_candidate = workspace_clusters.get(cluster_id)
                if workspace_candidate is None:
                    continue
                if (
                    workspace_candidate.owner_source_id == source.source_id
                    and cluster_id in event_clusters
                ):
                    continue
                heuristic_similarity = self._score_event_cluster_candidate(
                    cluster_record=workspace_candidate.cluster_record,
                    title_text=title_text,
                    signature_text=event_signature,
                    published_at=published_at,
                    window_days=source.content_lifecycle.event_cluster_window_days,
                )
                combined_similarity = self._combine_event_cluster_similarity(
                    heuristic_similarity=heuristic_similarity,
                    vector_similarity=vector_similarity,
                )
                if combined_similarity <= 0:
                    continue
                existing = candidate_scores.get(cluster_id)
                if (
                    existing is not None
                    and existing.get("similarity", 0.0) >= combined_similarity
                ):
                    continue
                candidate_scores[cluster_id] = {
                    "owner_source_id": workspace_candidate.owner_source_id,
                    "cluster_record": workspace_candidate.cluster_record,
                    "heuristic_similarity": heuristic_similarity,
                    "vector_similarity": vector_similarity,
                    "similarity": combined_similarity,
                }

        if not candidate_scores:
            return None

        candidates = sorted(
            candidate_scores.items(),
            key=lambda item: float(item[1].get("similarity", 0.0)),
            reverse=True,
        )
        best_cluster_id, best_candidate = candidates[0]
        best_similarity = float(best_candidate.get("similarity", 0.0))
        best_cluster = best_candidate["cluster_record"]
        min_similarity = source.content_lifecycle.event_cluster_min_similarity
        if best_similarity >= min_similarity:
            return EventClusterMatch(
                cluster_id=best_cluster_id,
                owner_source_id=str(best_candidate.get("owner_source_id") or source.source_id),
                similarity=best_similarity,
            )

        if mode != "heuristic_llm" or not self._utility_llm_available():
            return None

        review_floor = max(0.25, min(0.65, min_similarity * 0.5))
        if best_similarity < review_floor:
            return None

        matched = await self._llm_confirms_event_cluster(
            page_key=page_key,
            title=title,
            content=content,
            published_at=published_at,
            cluster_record=best_cluster,
            heuristic_similarity=best_similarity,
        )
        if not matched:
            return None
        return EventClusterMatch(
            cluster_id=best_cluster_id,
            owner_source_id=str(best_candidate.get("owner_source_id") or source.source_id),
            similarity=best_similarity,
            adjudicated_by_llm=True,
        )

    async def _llm_confirms_event_cluster(
        self,
        *,
        page_key: str,
        title: str | None,
        content: str,
        published_at: str | None,
        cluster_record: EventClusterRecord,
        heuristic_similarity: float,
    ) -> bool:
        if not self._utility_llm_available():
            return False

        content_preview = " ".join((content or "").strip().split())[:500]
        user_prompt = json.dumps(
            {
                "task": "Decide whether the candidate article is the same underlying short-lived news event as the existing event cluster. Treat follow-up corrections and updated versions of the same event as a match. Treat different events in the same sector as not a match.",
                "candidate_article": {
                    "url": page_key,
                    "title": (title or "").strip(),
                    "published_at": published_at,
                    "content_preview": content_preview,
                },
                "existing_event_cluster": {
                    "cluster_id": cluster_record.cluster_id,
                    "headline": cluster_record.headline,
                    "published_at": cluster_record.published_at,
                    "representative_item_key": cluster_record.representative_item_key,
                    "signature_text": cluster_record.signature_text[:500],
                },
                "heuristic_similarity": round(heuristic_similarity, 4),
                "response_schema": {
                    "same_event": True,
                    "confidence": 0.0,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            payload = await self.utility_llm_client.complete_json(
                system_prompt=(
                    "You are a strict news-event clustering judge. "
                    "Return only JSON."
                ),
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=220,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "Utility LLM event-cluster review failed for '%s' vs '%s': %s",
                page_key,
                cluster_record.cluster_id,
                exc,
            )
            return False
        return bool(payload.get("same_event"))

    @classmethod
    def _build_event_signature_text(
        cls,
        *,
        title: str | None,
        content: str,
    ) -> str:
        title_tokens = cls._normalize_feed_fingerprint_text(title).split()
        content_tokens = cls._normalize_feed_fingerprint_text(content).split()
        merged_tokens = cls._unique_preserve_order(
            [
                *title_tokens[:EVENT_TITLE_TOKEN_LIMIT],
                *content_tokens[:EVENT_SIGNATURE_TOKEN_LIMIT],
            ]
        )
        return " ".join(merged_tokens[:EVENT_SIGNATURE_TOKEN_LIMIT])

    @classmethod
    def _score_event_cluster_candidate(
        cls,
        *,
        cluster_record: EventClusterRecord,
        title_text: str,
        signature_text: str,
        published_at: str | None,
        window_days: float,
    ) -> float:
        if window_days > 0 and cls._cluster_is_outside_time_window(
            cluster_record=cluster_record,
            published_at=published_at,
            window_days=window_days,
        ):
            return 0.0

        title_similarity = cls._token_similarity(
            title_text.split(),
            cls._normalize_feed_fingerprint_text(cluster_record.headline).split(),
        )
        signature_similarity = cls._token_similarity(
            signature_text.split(),
            cluster_record.signature_text.split(),
        )
        score = (0.5 * title_similarity) + (0.5 * signature_similarity)
        if (
            title_text
            and cluster_record.headline
            and (
                title_text in cls._normalize_feed_fingerprint_text(cluster_record.headline)
                or cls._normalize_feed_fingerprint_text(cluster_record.headline)
                in title_text
            )
        ):
            score += 0.08
        if title_similarity >= 0.8 and signature_similarity >= 0.35:
            score += 0.1
        if title_similarity >= 0.7 and signature_similarity >= 0.3:
            score += 0.18
        return min(1.0, score)

    @staticmethod
    def _cluster_is_outside_time_window(
        *,
        cluster_record: EventClusterRecord,
        published_at: str | None,
        window_days: float,
    ) -> bool:
        if window_days <= 0:
            return False
        candidate_time = _parse_iso8601(published_at) or datetime.now(timezone.utc)
        cluster_time = _parse_iso8601(cluster_record.published_at) or _parse_iso8601(
            cluster_record.updated_at
        )
        if cluster_time is None:
            return False
        return abs((candidate_time - cluster_time).total_seconds()) > (
            window_days * 86400.0
        )

    @staticmethod
    def _token_similarity(left_tokens: list[str], right_tokens: list[str]) -> float:
        left = {token.strip() for token in left_tokens if token.strip()}
        right = {token.strip() for token in right_tokens if token.strip()}
        if not left or not right:
            return 0.0
        overlap = left & right
        if not overlap:
            return 0.0
        coverage = len(overlap) / max(1, min(len(left), len(right)))
        jaccard = len(overlap) / max(1, len(left | right))
        return (0.65 * coverage) + (0.35 * jaccard)

    @staticmethod
    def _unique_preserve_order(values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        return normalized

    @classmethod
    def _create_event_cluster_id(
        cls,
        *,
        source: MonitoredSource,
        page_key: str,
    ) -> str:
        return compute_mdhash_id(
            f"crawler-event:{source.source_id}:{page_key.strip()}"
        )

    @classmethod
    def _update_event_cluster_record(
        cls,
        *,
        event_clusters: dict[str, EventClusterRecord],
        cluster_id: str,
        page_key: str,
        title: str | None,
        content: str,
        content_fingerprint: str,
        published_at: str | None,
        active_doc_id: str | None,
        similarity: float,
        adjudicated_by_llm: bool,
        updated_at: str,
    ) -> dict[str, EventClusterRecord]:
        current = event_clusters.get(cluster_id)
        member_item_keys = []
        if current is not None:
            member_item_keys.extend(current.member_item_keys)
        member_item_keys.append(page_key)
        updated_record = EventClusterRecord(
            cluster_id=cluster_id,
            headline=(title or "").strip() or (current.headline if current else ""),
            signature_text=cls._build_event_signature_text(title=title, content=content),
            content_fingerprint=content_fingerprint
            or (current.content_fingerprint if current else ""),
            published_at=published_at or (current.published_at if current else None),
            updated_at=updated_at,
            representative_item_key=page_key,
            active_doc_id=active_doc_id or (current.active_doc_id if current else None),
            member_item_keys=cls._unique_preserve_order(member_item_keys),
            last_similarity=max(
                similarity,
                current.last_similarity if current is not None else 0.0,
            ),
            adjudicated_by_llm=bool(
                adjudicated_by_llm or (current.adjudicated_by_llm if current else False)
            ),
        )
        updated_clusters = dict(event_clusters)
        updated_clusters[cluster_id] = updated_record
        return updated_clusters

    @classmethod
    def _canonicalize_event_clusters(
        cls,
        event_clusters: dict[str, EventClusterRecord],
        *,
        item_event_cluster_ids: dict[str, str],
    ) -> dict[str, EventClusterRecord]:
        normalized_clusters: dict[str, EventClusterRecord] = {}
        for cluster_id, cluster_record in event_clusters.items():
            record = (
                cluster_record
                if isinstance(cluster_record, EventClusterRecord)
                else EventClusterRecord.from_dict(cluster_id, cluster_record)
            )
            member_item_keys = cls._canonicalize_feed_item_keys(record.member_item_keys)
            for item_key, item_cluster_id in item_event_cluster_ids.items():
                if item_cluster_id != cluster_id:
                    continue
                canonical_key = cls._canonicalize_feed_item_key(item_key)
                if canonical_key and canonical_key not in member_item_keys:
                    member_item_keys.append(canonical_key)
            representative_item_key = cls._canonicalize_feed_item_key(
                record.representative_item_key
            )
            if not representative_item_key and member_item_keys:
                representative_item_key = member_item_keys[0]
            normalized_clusters[str(cluster_id).strip()] = replace(
                record,
                representative_item_key=representative_item_key or None,
                member_item_keys=member_item_keys,
            )
        return normalized_clusters

    @classmethod
    def _apply_event_cluster_retention(
        cls,
        *,
        item_event_cluster_ids: dict[str, str],
        event_clusters: dict[str, EventClusterRecord],
        retained_item_keys: list[str],
    ) -> tuple[dict[str, str], dict[str, EventClusterRecord], list[str]]:
        retained_set = {str(key).strip() for key in retained_item_keys if str(key).strip()}
        pruned_item_event_cluster_ids = {
            key: value
            for key, value in item_event_cluster_ids.items()
            if key in retained_set and str(value or "").strip()
        }
        pruned_event_clusters: dict[str, EventClusterRecord] = {}
        retired_doc_ids: list[str] = []
        for cluster_id, cluster_record in event_clusters.items():
            retained_members = [
                item_key
                for item_key in cluster_record.member_item_keys
                if item_key in retained_set
            ]
            retained_members.extend(
                [
                    item_key
                    for item_key, item_cluster_id in pruned_item_event_cluster_ids.items()
                    if item_cluster_id == cluster_id and item_key not in retained_members
                ]
            )
            retained_members = cls._unique_preserve_order(retained_members)
            if not retained_members:
                if cluster_record.active_doc_id:
                    retired_doc_ids.append(cluster_record.active_doc_id)
                continue
            representative_item_key = cluster_record.representative_item_key
            if representative_item_key not in retained_members:
                representative_item_key = retained_members[0]
            pruned_event_clusters[cluster_id] = replace(
                cluster_record,
                representative_item_key=representative_item_key,
                member_item_keys=retained_members,
            )
        return pruned_item_event_cluster_ids, pruned_event_clusters, retired_doc_ids

    @staticmethod
    def _canonicalize_feed_item_key(value: str | None) -> str:
        raw_value = (value or "").strip()
        if not raw_value:
            return ""
        return canonicalize_url(raw_value) or raw_value

    @classmethod
    def _canonicalize_feed_item_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            key = cls._canonicalize_feed_item_key(item)
            if not key or key in seen:
                continue
            normalized.append(key)
            seen.add(key)
        return normalized

    @classmethod
    def _canonicalize_feed_url_map(
        cls,
        values: dict[str, str],
        *,
        preferred_order: list[str] | None = None,
    ) -> dict[str, str]:
        ordered_raw_keys: list[str] = []
        seen_raw: set[str] = set()
        for candidate in [*(preferred_order or []), *values.keys()]:
            raw_key = (candidate or "").strip()
            if not raw_key or raw_key in seen_raw or raw_key not in values:
                continue
            ordered_raw_keys.append(raw_key)
            seen_raw.add(raw_key)

        normalized: dict[str, str] = {}
        for raw_key in ordered_raw_keys:
            key = cls._canonicalize_feed_item_key(raw_key)
            value = str(values.get(raw_key) or "").strip()
            if not key or not value or key in normalized:
                continue
            normalized[key] = value
        return normalized

    @staticmethod
    def _select_newer_timestamp(
        current_value: str | None,
        candidate_value: str | None,
    ) -> str | None:
        current_parsed = _parse_iso8601(current_value)
        candidate_parsed = _parse_iso8601(candidate_value)
        if candidate_parsed is None:
            return current_value
        if current_parsed is None or candidate_parsed > current_parsed:
            return candidate_value
        return current_value

    @staticmethod
    def _build_fingerprint_owner_index(
        recent_item_keys: list[str],
        *,
        item_content_fingerprints: dict[str, str],
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        for key in [*recent_item_keys, *item_content_fingerprints.keys()]:
            normalized_key = (key or "").strip()
            fingerprint = str(item_content_fingerprints.get(normalized_key) or "").strip()
            if not normalized_key or not fingerprint or fingerprint in owners:
                continue
            owners[fingerprint] = normalized_key
        return owners

    @staticmethod
    def _normalize_feed_fingerprint_text(value: str | None) -> str:
        normalized = URL_TEXT_PATTERN.sub(" ", value or "")
        normalized = normalized.lower()
        tokens = FEED_FINGERPRINT_TOKEN_PATTERN.findall(normalized)
        return " ".join(tokens)

    @classmethod
    def _build_feed_content_fingerprint(
        cls,
        *,
        title: str | None,
        content: str,
        source: MonitoredSource,
    ) -> str:
        resolved_mode = source.feed_dedup.resolved_mode()
        if resolved_mode == "off":
            return ""
        normalized_content = cls._normalize_feed_fingerprint_text(content)
        if resolved_mode == "content_hash":
            return compute_mdhash_id(normalized_content or (content or "").strip())

        signature_text = normalized_content
        if not signature_text:
            signature_text = cls._normalize_feed_fingerprint_text(title)
        if not signature_text:
            return ""
        tokens = signature_text.split()
        limited_tokens = tokens[: source.feed_dedup.signature_token_limit]
        return compute_mdhash_id(" ".join(limited_tokens))

    @staticmethod
    def _merge_item_content_fingerprints(
        previous_item_content_fingerprints: dict[str, str],
        current_item_content_fingerprints: dict[str, str],
    ) -> dict[str, str]:
        merged = {
            str(key): str(value).strip()
            for key, value in previous_item_content_fingerprints.items()
            if str(key).strip() and str(value).strip()
        }
        for key, value in current_item_content_fingerprints.items():
            normalized_key = str(key).strip()
            normalized_value = str(value).strip()
            if not normalized_key or not normalized_value:
                continue
            merged[normalized_key] = normalized_value
        return merged

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
            normalized_url = self._canonicalize_feed_item_key(item.url)
            if not normalized_url or normalized_url in seen_urls:
                continue
            target_urls.append(normalized_url)
            seen_urls.add(normalized_url)
            if item.published_at:
                feed_item_published_at[normalized_url] = item.published_at
            if len(target_urls) >= page_limit:
                break

        limited_urls = target_urls[:page_limit]
        pages = await self.crawler_adapter.crawl_urls(
            limited_urls,
            max_pages=page_limit,
        )
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
        item_content_fingerprints: dict[str, str],
    ) -> tuple[dict[str, str], list[str], dict[str, str], dict[str, str]]:
        policy = source.feed_retention
        if source.resolved_source_type() != "feed":
            return hashes, recent_item_keys, item_published_at, item_content_fingerprints

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
        retained_item_content_fingerprints = {
            key: value
            for key, value in item_content_fingerprints.items()
            if key in retained_set
        }
        return (
            retained_hashes,
            retained_keys,
            retained_item_published_at,
            retained_item_content_fingerprints,
        )

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
        feed_deduplicated_count: int,
        superseded_count: int,
        deleted_doc_count: int,
    ) -> str:
        if source.resolved_source_type() != "feed":
            summary = (
                f"Polled {requested_count} page(s), {success_count} succeeded, "
                f"{ingested_count} ingested"
            )
        else:
            summary = (
                f"Discovered {feed_discovered_count} feed item(s), filtered {feed_filtered_count}, "
                f"deduplicated {feed_deduplicated_count}, crawled {requested_count}, "
                f"{success_count} succeeded, {ingested_count} ingested"
            )
        lifecycle_parts: list[str] = []
        if superseded_count > 0:
            lifecycle_parts.append(f"{superseded_count} superseded")
        if deleted_doc_count > 0:
            lifecycle_parts.append(f"{deleted_doc_count} deleted")
        if lifecycle_parts:
            return f"{summary}, lifecycle: {', '.join(lifecycle_parts)}"
        return summary

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
