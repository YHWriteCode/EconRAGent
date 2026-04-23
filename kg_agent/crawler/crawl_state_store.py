from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class EventClusterRecord:
    cluster_id: str
    headline: str = ""
    summary: str = ""
    signature_text: str = ""
    content_fingerprint: str = ""
    published_at: str | None = None
    updated_at: str | None = None
    representative_item_key: str | None = None
    active_doc_id: str | None = None
    member_item_keys: list[str] = field(default_factory=list)
    last_similarity: float = 0.0
    adjudicated_by_llm: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        cluster_id: str,
        payload: dict[str, object] | None,
    ) -> "EventClusterRecord":
        data = payload if isinstance(payload, dict) else {}
        raw_member_item_keys = (
            data.get("member_item_keys")
            if isinstance(data.get("member_item_keys"), list)
            else []
        )
        return cls(
            cluster_id=str(cluster_id or "").strip(),
            headline=str(data.get("headline") or "").strip(),
            summary=str(data.get("summary") or "").strip(),
            signature_text=str(data.get("signature_text") or "").strip(),
            content_fingerprint=str(data.get("content_fingerprint") or "").strip(),
            published_at=str(data.get("published_at") or "").strip() or None,
            updated_at=str(data.get("updated_at") or "").strip() or None,
            representative_item_key=str(data.get("representative_item_key") or "").strip()
            or None,
            active_doc_id=str(data.get("active_doc_id") or "").strip() or None,
            member_item_keys=[
                str(item).strip() for item in raw_member_item_keys if str(item).strip()
            ],
            last_similarity=max(0.0, float(data.get("last_similarity") or 0.0)),
            adjudicated_by_llm=bool(data.get("adjudicated_by_llm", False)),
        )


@dataclass
class CrawlStateRecord:
    source_id: str
    last_crawled_at: str | None = None
    last_content_hashes: dict[str, str] = field(default_factory=dict)
    recent_item_keys: list[str] = field(default_factory=list)
    item_published_at: dict[str, str] = field(default_factory=dict)
    item_content_fingerprints: dict[str, str] = field(default_factory=dict)
    item_active_doc_ids: dict[str, str] = field(default_factory=dict)
    item_event_cluster_ids: dict[str, str] = field(default_factory=dict)
    event_clusters: dict[str, EventClusterRecord] = field(default_factory=dict)
    doc_expires_at: dict[str, str] = field(default_factory=dict)
    last_status: str = "never_run"
    consecutive_failures: int = 0
    consecutive_no_change: int = 0
    total_ingested_count: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CrawlStateRecord":
        raw_hashes = payload.get("last_content_hashes")
        hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
        raw_recent_items = payload.get("recent_item_keys")
        recent_items = raw_recent_items if isinstance(raw_recent_items, list) else []
        raw_item_published_at = payload.get("item_published_at")
        item_published_at = (
            raw_item_published_at if isinstance(raw_item_published_at, dict) else {}
        )
        raw_item_content_fingerprints = payload.get("item_content_fingerprints")
        item_content_fingerprints = (
            raw_item_content_fingerprints
            if isinstance(raw_item_content_fingerprints, dict)
            else {}
        )
        raw_item_active_doc_ids = payload.get("item_active_doc_ids")
        item_active_doc_ids = (
            raw_item_active_doc_ids if isinstance(raw_item_active_doc_ids, dict) else {}
        )
        raw_item_event_cluster_ids = payload.get("item_event_cluster_ids")
        item_event_cluster_ids = (
            raw_item_event_cluster_ids
            if isinstance(raw_item_event_cluster_ids, dict)
            else {}
        )
        raw_event_clusters = payload.get("event_clusters")
        event_clusters = raw_event_clusters if isinstance(raw_event_clusters, dict) else {}
        raw_doc_expires_at = payload.get("doc_expires_at")
        doc_expires_at = raw_doc_expires_at if isinstance(raw_doc_expires_at, dict) else {}
        return cls(
            source_id=str(payload.get("source_id") or ""),
            last_crawled_at=str(payload.get("last_crawled_at") or "").strip() or None,
            last_content_hashes={
                str(key): str(value)
                for key, value in hashes.items()
                if key is not None and value is not None
            },
            recent_item_keys=[
                str(item).strip()
                for item in recent_items
                if str(item).strip()
            ],
            item_published_at={
                str(key): str(value).strip()
                for key, value in item_published_at.items()
                if key is not None and str(value).strip()
            },
            item_content_fingerprints={
                str(key): str(value).strip()
                for key, value in item_content_fingerprints.items()
                if key is not None and str(value).strip()
            },
            item_active_doc_ids={
                str(key): str(value).strip()
                for key, value in item_active_doc_ids.items()
                if key is not None and str(value).strip()
            },
            item_event_cluster_ids={
                str(key): str(value).strip()
                for key, value in item_event_cluster_ids.items()
                if key is not None and str(value).strip()
            },
            event_clusters={
                cluster_key: EventClusterRecord.from_dict(cluster_key, cluster_payload)
                for cluster_key, cluster_payload in event_clusters.items()
                if str(cluster_key).strip()
            },
            doc_expires_at={
                str(key): str(value).strip()
                for key, value in doc_expires_at.items()
                if key is not None and str(value).strip()
            },
            last_status=str(payload.get("last_status") or "never_run"),
            consecutive_failures=max(0, int(payload.get("consecutive_failures") or 0)),
            consecutive_no_change=max(0, int(payload.get("consecutive_no_change") or 0)),
            total_ingested_count=max(0, int(payload.get("total_ingested_count") or 0)),
            last_error=str(payload.get("last_error") or "").strip() or None,
        )


class CrawlStateStore(ABC):
    @abstractmethod
    async def list_records(self) -> list[CrawlStateRecord]:
        raise NotImplementedError

    @abstractmethod
    async def get_record(self, source_id: str) -> CrawlStateRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def put_record(self, record: CrawlStateRecord) -> CrawlStateRecord:
        raise NotImplementedError

    @abstractmethod
    async def remove_record(self, source_id: str) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def file_path(self) -> str | None:
        raise NotImplementedError


class JsonCrawlStateStore(CrawlStateStore):
    def __init__(self, file_path: str | None = None):
        self._file_path = (file_path or "").strip() or None
        self._records: dict[str, CrawlStateRecord] = {}
        self._loaded = False
        self._last_loaded_mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    @property
    def file_path(self) -> str | None:
        return self._file_path

    async def list_records(self) -> list[CrawlStateRecord]:
        async with self._lock:
            await self._ensure_loaded_locked()
            return [
                CrawlStateRecord.from_dict(item.to_dict())
                for item in sorted(self._records.values(), key=lambda value: value.source_id)
            ]

    async def get_record(self, source_id: str) -> CrawlStateRecord | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            record = self._records.get(source_id)
            return None if record is None else CrawlStateRecord.from_dict(record.to_dict())

    async def put_record(self, record: CrawlStateRecord) -> CrawlStateRecord:
        normalized = CrawlStateRecord.from_dict(record.to_dict())
        async with self._lock:
            await self._ensure_loaded_locked()
            self._records[normalized.source_id] = normalized
            await self._persist_locked()
            return CrawlStateRecord.from_dict(normalized.to_dict())

    async def remove_record(self, source_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded_locked()
            removed = self._records.pop(source_id, None) is not None
            if removed:
                await self._persist_locked()
            return removed

    async def _ensure_loaded_locked(self) -> None:
        if not self._file_path:
            self._loaded = True
            return

        path = Path(self._file_path)
        current_mtime_ns = self._read_mtime_ns(path)
        if self._loaded and current_mtime_ns == self._last_loaded_mtime_ns:
            return
        self._loaded = True
        self._last_loaded_mtime_ns = current_mtime_ns
        self._records = {}
        if current_mtime_ns is None:
            return

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(payload, list):
            return

        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                record = CrawlStateRecord.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._records[record.source_id] = record

    async def _persist_locked(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{id(self)}.tmp"
        )
        payload = [
            record.to_dict()
            for record in sorted(self._records.values(), key=lambda value: value.source_id)
        ]
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
        self._last_loaded_mtime_ns = self._read_mtime_ns(path)

    @staticmethod
    def _read_mtime_ns(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None


class SqliteCrawlStateStore(CrawlStateStore):
    def __init__(self, db_path: str):
        self._db_path = (db_path or "").strip()
        if not self._db_path:
            raise ValueError("SqliteCrawlStateStore requires a db_path")
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def file_path(self) -> str | None:
        return self._db_path

    async def list_records(self) -> list[CrawlStateRecord]:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._list_records_sync)

    async def get_record(self, source_id: str) -> CrawlStateRecord | None:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._get_record_sync, source_id)

    async def put_record(self, record: CrawlStateRecord) -> CrawlStateRecord:
        normalized = CrawlStateRecord.from_dict(record.to_dict())
        async with self._lock:
            await self._ensure_initialized_locked()
            await asyncio.to_thread(self._put_record_sync, normalized)
            return CrawlStateRecord.from_dict(normalized.to_dict())

    async def remove_record(self, source_id: str) -> bool:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._remove_record_sync, source_id)

    async def _ensure_initialized_locked(self) -> None:
        if self._initialized:
            return
        await asyncio.to_thread(self._initialize_sync)
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _initialize_sync(self) -> None:
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crawl_state_records (
                    source_id TEXT PRIMARY KEY,
                    last_crawled_at TEXT,
                    last_content_hashes_json TEXT NOT NULL,
                    recent_item_keys_json TEXT NOT NULL DEFAULT '[]',
                    item_published_at_json TEXT NOT NULL DEFAULT '{}',
                    item_content_fingerprints_json TEXT NOT NULL DEFAULT '{}',
                    item_active_doc_ids_json TEXT NOT NULL DEFAULT '{}',
                    item_event_cluster_ids_json TEXT NOT NULL DEFAULT '{}',
                    event_clusters_json TEXT NOT NULL DEFAULT '{}',
                    doc_expires_at_json TEXT NOT NULL DEFAULT '{}',
                    last_status TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    consecutive_no_change INTEGER NOT NULL DEFAULT 0,
                    total_ingested_count INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )
            self._ensure_columns_sync(conn)
            conn.commit()

    @staticmethod
    def _ensure_columns_sync(conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(crawl_state_records)").fetchall()
        }
        if "consecutive_no_change" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN consecutive_no_change INTEGER NOT NULL DEFAULT 0"
            )
        if "recent_item_keys_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN recent_item_keys_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "item_published_at_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN item_published_at_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "item_content_fingerprints_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN item_content_fingerprints_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "item_active_doc_ids_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN item_active_doc_ids_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "item_event_cluster_ids_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN item_event_cluster_ids_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "event_clusters_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN event_clusters_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "doc_expires_at_json" not in columns:
            conn.execute(
                "ALTER TABLE crawl_state_records ADD COLUMN doc_expires_at_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _list_records_sync(self) -> list[CrawlStateRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_id, last_crawled_at, last_content_hashes_json,
                       recent_item_keys_json, item_published_at_json,
                       item_content_fingerprints_json,
                       item_active_doc_ids_json, item_event_cluster_ids_json,
                       event_clusters_json, doc_expires_at_json,
                       last_status, consecutive_failures,
                       consecutive_no_change,
                       total_ingested_count, last_error
                FROM crawl_state_records
                ORDER BY source_id ASC
                """
            ).fetchall()
        return [self._row_to_record(*row) for row in rows]

    def _get_record_sync(self, source_id: str) -> CrawlStateRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_id, last_crawled_at, last_content_hashes_json,
                       recent_item_keys_json, item_published_at_json,
                       item_content_fingerprints_json,
                       item_active_doc_ids_json, item_event_cluster_ids_json,
                       event_clusters_json, doc_expires_at_json,
                       last_status, consecutive_failures,
                       consecutive_no_change,
                       total_ingested_count, last_error
                FROM crawl_state_records
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        return None if row is None else self._row_to_record(*row)

    def _put_record_sync(self, record: CrawlStateRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_state_records (
                    source_id, last_crawled_at, last_content_hashes_json,
                    recent_item_keys_json, item_published_at_json,
                    item_content_fingerprints_json,
                    item_active_doc_ids_json, item_event_cluster_ids_json,
                    event_clusters_json, doc_expires_at_json,
                    last_status, consecutive_failures,
                    consecutive_no_change, total_ingested_count, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_crawled_at = excluded.last_crawled_at,
                    last_content_hashes_json = excluded.last_content_hashes_json,
                    recent_item_keys_json = excluded.recent_item_keys_json,
                    item_published_at_json = excluded.item_published_at_json,
                    item_content_fingerprints_json = excluded.item_content_fingerprints_json,
                    item_active_doc_ids_json = excluded.item_active_doc_ids_json,
                    item_event_cluster_ids_json = excluded.item_event_cluster_ids_json,
                    event_clusters_json = excluded.event_clusters_json,
                    doc_expires_at_json = excluded.doc_expires_at_json,
                    last_status = excluded.last_status,
                    consecutive_failures = excluded.consecutive_failures,
                    consecutive_no_change = excluded.consecutive_no_change,
                    total_ingested_count = excluded.total_ingested_count,
                    last_error = excluded.last_error
                """,
                (
                    record.source_id,
                    record.last_crawled_at,
                    json.dumps(record.last_content_hashes, ensure_ascii=False),
                    json.dumps(record.recent_item_keys, ensure_ascii=False),
                    json.dumps(record.item_published_at, ensure_ascii=False),
                    json.dumps(record.item_content_fingerprints, ensure_ascii=False),
                    json.dumps(record.item_active_doc_ids, ensure_ascii=False),
                    json.dumps(record.item_event_cluster_ids, ensure_ascii=False),
                    json.dumps(
                        {
                            key: value.to_dict()
                            for key, value in record.event_clusters.items()
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(record.doc_expires_at, ensure_ascii=False),
                    record.last_status,
                    record.consecutive_failures,
                    record.consecutive_no_change,
                    record.total_ingested_count,
                    record.last_error,
                ),
            )
            conn.commit()

    def _remove_record_sync(self, source_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM crawl_state_records WHERE source_id = ?",
                (source_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_record(
        source_id: str,
        last_crawled_at: str | None,
        last_content_hashes_json: str,
        recent_item_keys_json: str,
        item_published_at_json: str,
        item_content_fingerprints_json: str,
        item_active_doc_ids_json: str,
        item_event_cluster_ids_json: str,
        event_clusters_json: str,
        doc_expires_at_json: str,
        last_status: str,
        consecutive_failures: int,
        consecutive_no_change: int,
        total_ingested_count: int,
        last_error: str | None,
    ) -> CrawlStateRecord:
        try:
            hashes = json.loads(last_content_hashes_json or "{}")
        except json.JSONDecodeError:
            hashes = {}
        if not isinstance(hashes, dict):
            hashes = {}
        try:
            recent_item_keys = json.loads(recent_item_keys_json or "[]")
        except json.JSONDecodeError:
            recent_item_keys = []
        if not isinstance(recent_item_keys, list):
            recent_item_keys = []
        try:
            item_published_at = json.loads(item_published_at_json or "{}")
        except json.JSONDecodeError:
            item_published_at = {}
        if not isinstance(item_published_at, dict):
            item_published_at = {}
        try:
            item_content_fingerprints = json.loads(
                item_content_fingerprints_json or "{}"
            )
        except json.JSONDecodeError:
            item_content_fingerprints = {}
        if not isinstance(item_content_fingerprints, dict):
            item_content_fingerprints = {}
        try:
            item_active_doc_ids = json.loads(item_active_doc_ids_json or "{}")
        except json.JSONDecodeError:
            item_active_doc_ids = {}
        if not isinstance(item_active_doc_ids, dict):
            item_active_doc_ids = {}
        try:
            item_event_cluster_ids = json.loads(item_event_cluster_ids_json or "{}")
        except json.JSONDecodeError:
            item_event_cluster_ids = {}
        if not isinstance(item_event_cluster_ids, dict):
            item_event_cluster_ids = {}
        try:
            event_clusters = json.loads(event_clusters_json or "{}")
        except json.JSONDecodeError:
            event_clusters = {}
        if not isinstance(event_clusters, dict):
            event_clusters = {}
        try:
            doc_expires_at = json.loads(doc_expires_at_json or "{}")
        except json.JSONDecodeError:
            doc_expires_at = {}
        if not isinstance(doc_expires_at, dict):
            doc_expires_at = {}
        return CrawlStateRecord(
            source_id=source_id,
            last_crawled_at=(last_crawled_at or "").strip() or None,
            last_content_hashes={
                str(key): str(value)
                for key, value in hashes.items()
                if key is not None and value is not None
            },
            recent_item_keys=[
                str(item).strip()
                for item in recent_item_keys
                if str(item).strip()
            ],
            item_published_at={
                str(key): str(value).strip()
                for key, value in item_published_at.items()
                if key is not None and str(value).strip()
            },
            item_content_fingerprints={
                str(key): str(value).strip()
                for key, value in item_content_fingerprints.items()
                if key is not None and str(value).strip()
            },
            item_active_doc_ids={
                str(key): str(value).strip()
                for key, value in item_active_doc_ids.items()
                if key is not None and str(value).strip()
            },
            item_event_cluster_ids={
                str(key): str(value).strip()
                for key, value in item_event_cluster_ids.items()
                if key is not None and str(value).strip()
            },
            event_clusters={
                cluster_key: EventClusterRecord.from_dict(cluster_key, cluster_payload)
                for cluster_key, cluster_payload in event_clusters.items()
                if str(cluster_key).strip()
            },
            doc_expires_at={
                str(key): str(value).strip()
                for key, value in doc_expires_at.items()
                if key is not None and str(value).strip()
            },
            last_status=last_status,
            consecutive_failures=int(consecutive_failures),
            consecutive_no_change=int(consecutive_no_change),
            total_ingested_count=int(total_ingested_count),
            last_error=(last_error or "").strip() or None,
        )


def build_crawl_state_store(
    *,
    backend: str,
    file_path: str | None = None,
    sqlite_path: str | None = None,
) -> CrawlStateStore:
    normalized_backend = (backend or "json").strip().lower() or "json"
    if normalized_backend == "sqlite":
        return SqliteCrawlStateStore(sqlite_path or "kg_agent_scheduler.sqlite3")
    return JsonCrawlStateStore(file_path)
