from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from hashlib import md5
from pathlib import Path


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

    def __post_init__(self) -> None:
        self.source_id = (self.source_id or "").strip()
        self.name = (self.name or "").strip()
        self.category = (self.category or "general").strip() or "general"
        self.workspace = (self.workspace or "").strip() or None
        self.urls = _normalize_urls(self.urls)
        self.interval_seconds = max(1, int(self.interval_seconds))
        self.max_pages = max(1, int(self.max_pages))
        self.enabled = bool(self.enabled)

        if not self.source_id:
            self.source_id = _generate_source_id(self.name, self.urls)
        if not self.name:
            raise ValueError("Source name must be non-empty")
        if not self.urls:
            raise ValueError("Source urls must contain at least one URL")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

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
        if self._loaded:
            return
        self._loaded = True
        if not self._file_path:
            return

        path = Path(self._file_path)
        if not path.exists():
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
        payload = [
            source.to_dict()
            for source in sorted(self._sources.values(), key=lambda value: value.source_id)
        ]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
