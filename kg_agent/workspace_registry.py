from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-")
    return collapsed.lower() or "workspace"


def generate_workspace_id(display_name: str) -> str:
    return f"{_slugify(display_name)}-{uuid.uuid4().hex[:8]}"


@dataclass
class WorkspaceRecord:
    workspace_id: str
    display_name: str
    created_at: str
    updated_at: str
    description: str | None = None
    archived: bool = False

    def __post_init__(self) -> None:
        self.workspace_id = (self.workspace_id or "").strip()
        self.display_name = (self.display_name or "").strip()
        self.created_at = (self.created_at or "").strip() or _utcnow_iso()
        self.updated_at = (self.updated_at or "").strip() or self.created_at
        self.description = (self.description or "").strip() or None
        self.archived = bool(self.archived)
        if not self.workspace_id:
            raise ValueError("workspace_id must be non-empty")
        if not self.display_name:
            raise ValueError("display_name must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "WorkspaceRecord":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            workspace_id=str(data.get("workspace_id") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            created_at=str(data.get("created_at") or "").strip() or _utcnow_iso(),
            updated_at=str(data.get("updated_at") or "").strip() or _utcnow_iso(),
            description=str(data.get("description") or "").strip() or None,
            archived=bool(data.get("archived", False)),
        )


class WorkspaceRegistry(ABC):
    @abstractmethod
    async def list_workspaces(self) -> list[WorkspaceRecord]:
        raise NotImplementedError

    @abstractmethod
    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def upsert_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        raise NotImplementedError

    @abstractmethod
    async def remove_workspace(self, workspace_id: str) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def file_path(self) -> str | None:
        raise NotImplementedError


class InMemoryWorkspaceRegistry(WorkspaceRegistry):
    def __init__(self) -> None:
        self._records: dict[str, WorkspaceRecord] = {}
        self._lock = asyncio.Lock()

    @property
    def file_path(self) -> str | None:
        return None

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        async with self._lock:
            return [
                WorkspaceRecord.from_dict(item.to_dict())
                for item in sorted(
                    self._records.values(),
                    key=lambda value: value.updated_at,
                    reverse=True,
                )
            ]

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        async with self._lock:
            record = self._records.get((workspace_id or "").strip())
            return None if record is None else WorkspaceRecord.from_dict(record.to_dict())

    async def upsert_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        normalized = WorkspaceRecord.from_dict(workspace.to_dict())
        async with self._lock:
            self._records[normalized.workspace_id] = normalized
            return WorkspaceRecord.from_dict(normalized.to_dict())

    async def remove_workspace(self, workspace_id: str) -> bool:
        async with self._lock:
            return self._records.pop((workspace_id or "").strip(), None) is not None


class JsonWorkspaceRegistry(WorkspaceRegistry):
    def __init__(self, file_path: str | None = None):
        self._file_path = (file_path or "").strip() or None
        self._records: dict[str, WorkspaceRecord] = {}
        self._loaded = False
        self._last_loaded_mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    @property
    def file_path(self) -> str | None:
        return self._file_path

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        async with self._lock:
            await self._ensure_loaded_locked()
            return [
                WorkspaceRecord.from_dict(item.to_dict())
                for item in sorted(
                    self._records.values(),
                    key=lambda value: value.updated_at,
                    reverse=True,
                )
            ]

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            record = self._records.get((workspace_id or "").strip())
            return None if record is None else WorkspaceRecord.from_dict(record.to_dict())

    async def upsert_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        normalized = WorkspaceRecord.from_dict(workspace.to_dict())
        async with self._lock:
            await self._ensure_loaded_locked()
            self._records[normalized.workspace_id] = normalized
            await self._persist_locked()
            return WorkspaceRecord.from_dict(normalized.to_dict())

    async def remove_workspace(self, workspace_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded_locked()
            removed = self._records.pop((workspace_id or "").strip(), None) is not None
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
                record = WorkspaceRecord.from_dict(item)
            except (TypeError, ValueError):
                continue
            self._records[record.workspace_id] = record

    async def _persist_locked(self) -> None:
        if not self._file_path:
            return
        path = Path(self._file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.{id(self)}.tmp")
        payload = [
            record.to_dict()
            for record in sorted(
                self._records.values(),
                key=lambda value: value.updated_at,
                reverse=True,
            )
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


class SqliteWorkspaceRegistry(WorkspaceRegistry):
    def __init__(self, db_path: str):
        self._db_path = (db_path or "").strip()
        if not self._db_path:
            raise ValueError("SqliteWorkspaceRegistry requires a db_path")
        self._lock = asyncio.Lock()
        self._initialized = False

    @property
    def file_path(self) -> str | None:
        return self._db_path

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._list_workspaces_sync)

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._get_workspace_sync, workspace_id)

    async def upsert_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        normalized = WorkspaceRecord.from_dict(workspace.to_dict())
        async with self._lock:
            await self._ensure_initialized_locked()
            await asyncio.to_thread(self._upsert_workspace_sync, normalized)
            return WorkspaceRecord.from_dict(normalized.to_dict())

    async def remove_workspace(self, workspace_id: str) -> bool:
        async with self._lock:
            await self._ensure_initialized_locked()
            return await asyncio.to_thread(self._remove_workspace_sync, workspace_id)

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
                CREATE TABLE IF NOT EXISTS workspace_registry (
                    workspace_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_registry_updated_at ON workspace_registry(updated_at DESC)"
            )
            conn.commit()

    def _list_workspaces_sync(self) -> list[WorkspaceRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT workspace_id, display_name, description, created_at, updated_at, archived
                FROM workspace_registry
                ORDER BY updated_at DESC, workspace_id ASC
                """
            ).fetchall()
        return [
            WorkspaceRecord(
                workspace_id=str(row[0]),
                display_name=str(row[1]),
                description=str(row[2]).strip() or None if row[2] is not None else None,
                created_at=str(row[3]),
                updated_at=str(row[4]),
                archived=bool(row[5]),
            )
            for row in rows
        ]

    def _get_workspace_sync(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT workspace_id, display_name, description, created_at, updated_at, archived
                FROM workspace_registry
                WHERE workspace_id = ?
                """,
                ((workspace_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceRecord(
            workspace_id=str(row[0]),
            display_name=str(row[1]),
            description=str(row[2]).strip() or None if row[2] is not None else None,
            created_at=str(row[3]),
            updated_at=str(row[4]),
            archived=bool(row[5]),
        )

    def _upsert_workspace_sync(self, workspace: WorkspaceRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspace_registry (
                    workspace_id, display_name, description, created_at, updated_at, archived
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    description = excluded.description,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    archived = excluded.archived
                """,
                (
                    workspace.workspace_id,
                    workspace.display_name,
                    workspace.description,
                    workspace.created_at,
                    workspace.updated_at,
                    1 if workspace.archived else 0,
                ),
            )
            conn.commit()

    def _remove_workspace_sync(self, workspace_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM workspace_registry WHERE workspace_id = ?",
                ((workspace_id or "").strip(),),
            )
            conn.commit()
            return cursor.rowcount > 0


def build_workspace_registry(
    *,
    backend: str = "memory",
    file_path: str | None = None,
    sqlite_path: str | None = None,
) -> WorkspaceRegistry:
    normalized_backend = (backend or "memory").strip().lower() or "memory"
    if normalized_backend == "sqlite":
        return SqliteWorkspaceRegistry(
            sqlite_path or "kg_agent_workspaces.sqlite3"
        )
    if normalized_backend == "json":
        return JsonWorkspaceRegistry(file_path or "kg_agent_workspaces.json")
    return InMemoryWorkspaceRegistry()
