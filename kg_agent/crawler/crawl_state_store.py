from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CrawlStateRecord:
    source_id: str
    last_crawled_at: str | None = None
    last_content_hashes: dict[str, str] = field(default_factory=dict)
    last_status: str = "never_run"
    consecutive_failures: int = 0
    total_ingested_count: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CrawlStateRecord":
        raw_hashes = payload.get("last_content_hashes")
        hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
        return cls(
            source_id=str(payload.get("source_id") or ""),
            last_crawled_at=str(payload.get("last_crawled_at") or "").strip() or None,
            last_content_hashes={
                str(key): str(value)
                for key, value in hashes.items()
                if key is not None and value is not None
            },
            last_status=str(payload.get("last_status") or "never_run"),
            consecutive_failures=max(0, int(payload.get("consecutive_failures") or 0)),
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
                record = CrawlStateRecord.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._records[record.source_id] = record

    async def _persist_locked(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            record.to_dict()
            for record in sorted(self._records.values(), key=lambda value: value.source_id)
        ]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
