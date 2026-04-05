from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from hashlib import md5
from pathlib import Path


SOURCE_TYPES = {"auto", "page", "feed"}
SCHEDULE_MODES = {"auto", "fixed", "adaptive_feed"}
FEED_RETENTION_MODES = {"keep_all", "latest"}
FEED_PRIORITY_MODES = {"auto", "feed_order", "published_desc", "priority_score"}


def _normalize_urls(urls: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in urls:
        url = (item or "").strip()
        if not url or url in seen:
            continue
        normalized.append(url)
        seen.add(url)
    return normalized


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-")
    return collapsed.lower() or "source"


def _generate_source_id(name: str, urls: list[str]) -> str:
    seed = f"{name}|{'|'.join(urls)}"
    digest = md5(seed.encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(name)}-{digest}"


def _normalize_patterns(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        pattern = " ".join((item or "").strip().split())
        if not pattern:
            continue
        lowered = pattern.lower()
        if lowered in seen:
            continue
        normalized.append(pattern)
        seen.add(lowered)
    return normalized


def _normalize_domains(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = (item or "").strip().lower().strip(".")
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _looks_like_feed_url(url: str) -> bool:
    normalized = (url or "").strip().lower()
    return any(
        marker in normalized
        for marker in (
            "/feed",
            "/rss",
            "rss.xml",
            "atom.xml",
            "feed.xml",
            "format=rss",
            "format=atom",
        )
    )


@dataclass
class FeedFilterPolicy:
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    include_authors: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)
    include_categories: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    max_age_days: float = 0.0

    def __post_init__(self) -> None:
        self.include_patterns = _normalize_patterns(self.include_patterns)
        self.exclude_patterns = _normalize_patterns(self.exclude_patterns)
        self.include_authors = _normalize_patterns(self.include_authors)
        self.exclude_authors = _normalize_patterns(self.exclude_authors)
        self.include_categories = _normalize_patterns(self.include_categories)
        self.exclude_categories = _normalize_patterns(self.exclude_categories)
        self.allowed_domains = _normalize_domains(self.allowed_domains)
        self.blocked_domains = _normalize_domains(self.blocked_domains)
        self.max_age_days = max(0.0, float(self.max_age_days or 0.0))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "FeedFilterPolicy":
        if not isinstance(payload, dict):
            return cls()
        raw_include = (
            payload.get("include_patterns")
            if isinstance(payload.get("include_patterns"), list)
            else []
        )
        raw_exclude = (
            payload.get("exclude_patterns")
            if isinstance(payload.get("exclude_patterns"), list)
            else []
        )
        raw_include_authors = (
            payload.get("include_authors")
            if isinstance(payload.get("include_authors"), list)
            else []
        )
        raw_exclude_authors = (
            payload.get("exclude_authors")
            if isinstance(payload.get("exclude_authors"), list)
            else []
        )
        raw_include_categories = (
            payload.get("include_categories")
            if isinstance(payload.get("include_categories"), list)
            else []
        )
        raw_exclude_categories = (
            payload.get("exclude_categories")
            if isinstance(payload.get("exclude_categories"), list)
            else []
        )
        raw_allowed_domains = (
            payload.get("allowed_domains")
            if isinstance(payload.get("allowed_domains"), list)
            else []
        )
        raw_blocked_domains = (
            payload.get("blocked_domains")
            if isinstance(payload.get("blocked_domains"), list)
            else []
        )
        return cls(
            include_patterns=[str(item) for item in raw_include],
            exclude_patterns=[str(item) for item in raw_exclude],
            include_authors=[str(item) for item in raw_include_authors],
            exclude_authors=[str(item) for item in raw_exclude_authors],
            include_categories=[str(item) for item in raw_include_categories],
            exclude_categories=[str(item) for item in raw_exclude_categories],
            allowed_domains=[str(item) for item in raw_allowed_domains],
            blocked_domains=[str(item) for item in raw_blocked_domains],
            max_age_days=float(payload.get("max_age_days") or 0.0),
        )

    def is_active(self) -> bool:
        return bool(
            self.include_patterns
            or self.exclude_patterns
            or self.include_authors
            or self.exclude_authors
            or self.include_categories
            or self.exclude_categories
            or self.allowed_domains
            or self.blocked_domains
            or self.max_age_days > 0
        )


@dataclass
class FeedRetentionPolicy:
    mode: str = "keep_all"
    max_items: int = 0
    max_age_days: float = 0.0

    def __post_init__(self) -> None:
        self.mode = (self.mode or "keep_all").strip().lower() or "keep_all"
        self.max_items = max(0, int(self.max_items))
        self.max_age_days = max(0.0, float(self.max_age_days or 0.0))
        if self.mode not in FEED_RETENTION_MODES:
            raise ValueError(
                f"Unsupported feed retention mode '{self.mode}'. Expected one of: {sorted(FEED_RETENTION_MODES)}"
            )
        if self.mode == "latest" and self.max_items <= 0:
            raise ValueError("Feed retention mode 'latest' requires max_items > 0")
        if self.mode == "keep_all":
            self.max_items = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "FeedRetentionPolicy":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            mode=str(payload.get("mode") or "keep_all"),
            max_items=int(payload.get("max_items") or 0),
            max_age_days=float(payload.get("max_age_days") or 0.0),
        )


@dataclass
class FeedPriorityPolicy:
    mode: str = "auto"
    priority_patterns: list[str] = field(default_factory=list)
    preferred_domains: list[str] = field(default_factory=list)
    preferred_authors: list[str] = field(default_factory=list)
    preferred_categories: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mode = (self.mode or "auto").strip().lower() or "auto"
        self.priority_patterns = _normalize_patterns(self.priority_patterns)
        self.preferred_domains = _normalize_domains(self.preferred_domains)
        self.preferred_authors = _normalize_patterns(self.preferred_authors)
        self.preferred_categories = _normalize_patterns(self.preferred_categories)
        if self.mode not in FEED_PRIORITY_MODES:
            raise ValueError(
                f"Unsupported feed priority mode '{self.mode}'. Expected one of: {sorted(FEED_PRIORITY_MODES)}"
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "FeedPriorityPolicy":
        if not isinstance(payload, dict):
            return cls()
        raw_priority_patterns = (
            payload.get("priority_patterns")
            if isinstance(payload.get("priority_patterns"), list)
            else []
        )
        raw_preferred_domains = (
            payload.get("preferred_domains")
            if isinstance(payload.get("preferred_domains"), list)
            else []
        )
        raw_preferred_authors = (
            payload.get("preferred_authors")
            if isinstance(payload.get("preferred_authors"), list)
            else []
        )
        raw_preferred_categories = (
            payload.get("preferred_categories")
            if isinstance(payload.get("preferred_categories"), list)
            else []
        )
        return cls(
            mode=str(payload.get("mode") or "auto"),
            priority_patterns=[str(item) for item in raw_priority_patterns],
            preferred_domains=[str(item) for item in raw_preferred_domains],
            preferred_authors=[str(item) for item in raw_preferred_authors],
            preferred_categories=[str(item) for item in raw_preferred_categories],
        )

    def has_preference_signals(self) -> bool:
        return bool(
            self.priority_patterns
            or self.preferred_domains
            or self.preferred_authors
            or self.preferred_categories
        )

    def resolved_mode(self) -> str:
        if self.mode != "auto":
            return self.mode
        return "priority_score" if self.has_preference_signals() else "feed_order"

    def is_active(self) -> bool:
        return self.resolved_mode() != "feed_order"


@dataclass
class MonitoredSource:
    source_id: str
    name: str
    urls: list[str]
    category: str = "general"
    interval_seconds: int = 3600
    max_pages: int = 3
    enabled: bool = True
    workspace: str | None = None
    source_type: str = "auto"
    schedule_mode: str = "auto"
    feed_filter: FeedFilterPolicy = field(default_factory=FeedFilterPolicy)
    feed_retention: FeedRetentionPolicy = field(default_factory=FeedRetentionPolicy)
    feed_priority: FeedPriorityPolicy = field(default_factory=FeedPriorityPolicy)

    def __post_init__(self) -> None:
        self.source_id = (self.source_id or "").strip()
        self.name = (self.name or "").strip()
        self.category = (self.category or "general").strip() or "general"
        self.workspace = (self.workspace or "").strip() or None
        self.source_type = (self.source_type or "auto").strip().lower() or "auto"
        self.schedule_mode = (self.schedule_mode or "auto").strip().lower() or "auto"
        self.urls = _normalize_urls(self.urls)
        self.interval_seconds = max(1, int(self.interval_seconds))
        self.max_pages = max(1, int(self.max_pages))
        self.enabled = bool(self.enabled)
        if not isinstance(self.feed_filter, FeedFilterPolicy):
            self.feed_filter = FeedFilterPolicy.from_dict(self.feed_filter)
        if not isinstance(self.feed_retention, FeedRetentionPolicy):
            self.feed_retention = FeedRetentionPolicy.from_dict(self.feed_retention)
        if not isinstance(self.feed_priority, FeedPriorityPolicy):
            self.feed_priority = FeedPriorityPolicy.from_dict(self.feed_priority)

        if not self.source_id:
            self.source_id = _generate_source_id(self.name, self.urls)
        if not self.name:
            raise ValueError("Source name must be non-empty")
        if not self.urls:
            raise ValueError("Source urls must contain at least one URL")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(
                f"Unsupported source_type '{self.source_type}'. Expected one of: {sorted(SOURCE_TYPES)}"
            )
        if self.schedule_mode not in SCHEDULE_MODES:
            raise ValueError(
                f"Unsupported schedule_mode '{self.schedule_mode}'. Expected one of: {sorted(SCHEDULE_MODES)}"
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, object]:
        payload = self.to_dict()
        payload["resolved_source_type"] = self.resolved_source_type()
        payload["resolved_schedule_mode"] = self.resolved_schedule_mode()
        payload["resolved_feed_priority_mode"] = self.feed_priority.resolved_mode()
        return payload

    def resolved_source_type(self) -> str:
        if self.source_type != "auto":
            return self.source_type
        return "feed" if any(_looks_like_feed_url(url) for url in self.urls) else "page"

    def resolved_schedule_mode(self) -> str:
        if self.schedule_mode != "auto":
            return self.schedule_mode
        return "adaptive_feed" if self.resolved_source_type() == "feed" else "fixed"

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "MonitoredSource":
        urls = payload.get("urls") if isinstance(payload.get("urls"), list) else []
        return cls(
            source_id=str(payload.get("source_id") or ""),
            name=str(payload.get("name") or ""),
            urls=[str(item) for item in urls],
            category=str(payload.get("category") or "general"),
            interval_seconds=int(payload.get("interval_seconds") or 3600),
            max_pages=int(payload.get("max_pages") or 3),
            enabled=bool(payload.get("enabled", True)),
            workspace=str(payload.get("workspace") or "").strip() or None,
            source_type=str(payload.get("source_type") or "auto"),
            schedule_mode=str(payload.get("schedule_mode") or "auto"),
            feed_filter=FeedFilterPolicy.from_dict(payload.get("feed_filter")),
            feed_retention=FeedRetentionPolicy.from_dict(payload.get("feed_retention")),
            feed_priority=FeedPriorityPolicy.from_dict(payload.get("feed_priority")),
        )


class SourceRegistry(ABC):
    @abstractmethod
    async def list_sources(self) -> list[MonitoredSource]:
        raise NotImplementedError

    @abstractmethod
    async def get_source(self, source_id: str) -> MonitoredSource | None:
        raise NotImplementedError

    @abstractmethod
    async def upsert_source(self, source: MonitoredSource) -> MonitoredSource:
        raise NotImplementedError

    @abstractmethod
    async def remove_source(self, source_id: str) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def file_path(self) -> str | None:
        raise NotImplementedError

    @property
    def is_persistent(self) -> bool:
        return bool(self.file_path)


class JsonSourceRegistry(SourceRegistry):
    def __init__(self, file_path: str | None = None):
        self._file_path = (file_path or "").strip() or None
        self._sources: dict[str, MonitoredSource] = {}
        self._loaded = False
        self._last_loaded_mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    @property
    def file_path(self) -> str | None:
        return self._file_path

    async def list_sources(self) -> list[MonitoredSource]:
        async with self._lock:
            await self._ensure_loaded_locked()
            return [
                MonitoredSource.from_dict(item.to_dict())
                for item in sorted(self._sources.values(), key=lambda value: value.source_id)
            ]

    async def get_source(self, source_id: str) -> MonitoredSource | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            source = self._sources.get(source_id)
            return None if source is None else MonitoredSource.from_dict(source.to_dict())

    async def upsert_source(self, source: MonitoredSource) -> MonitoredSource:
        normalized = MonitoredSource.from_dict(source.to_dict())
        async with self._lock:
            await self._ensure_loaded_locked()
            self._sources[normalized.source_id] = normalized
            await self._persist_locked()
            return MonitoredSource.from_dict(normalized.to_dict())

    async def remove_source(self, source_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded_locked()
            removed = self._sources.pop(source_id, None) is not None
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
        self._sources = {}
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
                source = MonitoredSource.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._sources[source.source_id] = source

    async def _persist_locked(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{id(self)}.tmp"
        )
        payload = [
            source.to_dict()
            for source in sorted(self._sources.values(), key=lambda value: value.source_id)
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


class SqliteSourceRegistry(SourceRegistry):
    def __init__(self, db_path: str):
        self._db_path = (db_path or "").strip()
        if not self._db_path:
            raise ValueError("SqliteSourceRegistry requires a db_path")
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def file_path(self) -> str | None:
        return self._db_path

    async def list_sources(self) -> list[MonitoredSource]:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._list_sources_sync)

    async def get_source(self, source_id: str) -> MonitoredSource | None:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._get_source_sync, source_id)

    async def upsert_source(self, source: MonitoredSource) -> MonitoredSource:
        normalized = MonitoredSource.from_dict(source.to_dict())
        async with self._lock:
            await self._ensure_initialized_locked()
            await asyncio.to_thread(self._upsert_source_sync, normalized)
            return MonitoredSource.from_dict(normalized.to_dict())

    async def remove_source(self, source_id: str) -> bool:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._remove_source_sync, source_id)

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
                CREATE TABLE IF NOT EXISTS monitored_sources (
                    source_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    urls_json TEXT NOT NULL,
                    category TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    max_pages INTEGER NOT NULL,
                    enabled INTEGER NOT NULL,
                    workspace TEXT,
                    source_type TEXT NOT NULL DEFAULT 'auto',
                    schedule_mode TEXT NOT NULL DEFAULT 'auto',
                    feed_filter_json TEXT NOT NULL DEFAULT '{}',
                    feed_retention_json TEXT NOT NULL DEFAULT '{}',
                    feed_priority_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._ensure_columns_sync(conn)
            conn.commit()

    @staticmethod
    def _ensure_columns_sync(conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(monitored_sources)").fetchall()
        }
        if "source_type" not in columns:
            conn.execute(
                "ALTER TABLE monitored_sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'auto'"
            )
        if "schedule_mode" not in columns:
            conn.execute(
                "ALTER TABLE monitored_sources ADD COLUMN schedule_mode TEXT NOT NULL DEFAULT 'auto'"
            )
        if "feed_filter_json" not in columns:
            conn.execute(
                "ALTER TABLE monitored_sources ADD COLUMN feed_filter_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "feed_retention_json" not in columns:
            conn.execute(
                "ALTER TABLE monitored_sources ADD COLUMN feed_retention_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "feed_priority_json" not in columns:
            conn.execute(
                "ALTER TABLE monitored_sources ADD COLUMN feed_priority_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _list_sources_sync(self) -> list[MonitoredSource]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_id, name, urls_json, category, interval_seconds,
                       max_pages, enabled, workspace, source_type, schedule_mode,
                       feed_filter_json, feed_retention_json, feed_priority_json
                FROM monitored_sources
                ORDER BY source_id ASC
                """
            ).fetchall()
        return [self._row_to_source(*row) for row in rows]

    def _get_source_sync(self, source_id: str) -> MonitoredSource | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source_id, name, urls_json, category, interval_seconds,
                       max_pages, enabled, workspace, source_type, schedule_mode,
                       feed_filter_json, feed_retention_json, feed_priority_json
                FROM monitored_sources
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        return None if row is None else self._row_to_source(*row)

    def _upsert_source_sync(self, source: MonitoredSource) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitored_sources (
                    source_id, name, urls_json, category, interval_seconds,
                    max_pages, enabled, workspace, source_type, schedule_mode,
                    feed_filter_json, feed_retention_json, feed_priority_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    name = excluded.name,
                    urls_json = excluded.urls_json,
                    category = excluded.category,
                    interval_seconds = excluded.interval_seconds,
                    max_pages = excluded.max_pages,
                    enabled = excluded.enabled,
                    workspace = excluded.workspace,
                    source_type = excluded.source_type,
                    schedule_mode = excluded.schedule_mode,
                    feed_filter_json = excluded.feed_filter_json,
                    feed_retention_json = excluded.feed_retention_json,
                    feed_priority_json = excluded.feed_priority_json
                """,
                (
                    source.source_id,
                    source.name,
                    json.dumps(source.urls, ensure_ascii=False),
                    source.category,
                    source.interval_seconds,
                    source.max_pages,
                    int(source.enabled),
                    source.workspace,
                    source.source_type,
                    source.schedule_mode,
                    json.dumps(source.feed_filter.to_dict(), ensure_ascii=False),
                    json.dumps(source.feed_retention.to_dict(), ensure_ascii=False),
                    json.dumps(source.feed_priority.to_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()

    def _remove_source_sync(self, source_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM monitored_sources WHERE source_id = ?",
                (source_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_source(
        source_id: str,
        name: str,
        urls_json: str,
        category: str,
        interval_seconds: int,
        max_pages: int,
        enabled: int,
        workspace: str | None,
        source_type: str,
        schedule_mode: str,
        feed_filter_json: str,
        feed_retention_json: str,
        feed_priority_json: str,
    ) -> MonitoredSource:
        try:
            urls = json.loads(urls_json or "[]")
        except json.JSONDecodeError:
            urls = []
        if not isinstance(urls, list):
            urls = []
        try:
            feed_filter_payload = json.loads(feed_filter_json or "{}")
        except json.JSONDecodeError:
            feed_filter_payload = {}
        if not isinstance(feed_filter_payload, dict):
            feed_filter_payload = {}
        try:
            feed_retention_payload = json.loads(feed_retention_json or "{}")
        except json.JSONDecodeError:
            feed_retention_payload = {}
        if not isinstance(feed_retention_payload, dict):
            feed_retention_payload = {}
        try:
            feed_priority_payload = json.loads(feed_priority_json or "{}")
        except json.JSONDecodeError:
            feed_priority_payload = {}
        if not isinstance(feed_priority_payload, dict):
            feed_priority_payload = {}
        return MonitoredSource(
            source_id=source_id,
            name=name,
            urls=[str(item) for item in urls],
            category=category,
            interval_seconds=int(interval_seconds),
            max_pages=int(max_pages),
            enabled=bool(enabled),
            workspace=(workspace or "").strip() or None,
            source_type=(source_type or "auto").strip() or "auto",
            schedule_mode=(schedule_mode or "auto").strip() or "auto",
            feed_filter=FeedFilterPolicy.from_dict(feed_filter_payload),
            feed_retention=FeedRetentionPolicy.from_dict(feed_retention_payload),
            feed_priority=FeedPriorityPolicy.from_dict(feed_priority_payload),
        )


def build_source_registry(
    *,
    backend: str,
    file_path: str | None = None,
    sqlite_path: str | None = None,
) -> SourceRegistry:
    normalized_backend = (backend or "json").strip().lower() or "json"
    if normalized_backend == "sqlite":
        return SqliteSourceRegistry(sqlite_path or "kg_agent_scheduler.sqlite3")
    return JsonSourceRegistry(file_path)
