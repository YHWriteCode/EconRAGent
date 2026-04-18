from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class RuntimeRunStore:
    def __init__(
        self,
        *,
        run_store_db_path: Path,
        queue_max_attempts: int,
        queue_lease_timeout_s: float,
        utc_now: Callable[[], str],
    ) -> None:
        self.run_store_db_path = run_store_db_path
        self.queue_max_attempts = queue_max_attempts
        self.queue_lease_timeout_s = queue_lease_timeout_s
        self.utc_now = utc_now
        self.run_store: dict[str, dict[str, Any]] = {}
        self.db_initialized = False

    def run_store_connect(self) -> sqlite3.Connection:
        self.run_store_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.run_store_db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_initialized(self) -> None:
        if self.db_initialized:
            return
        with self.run_store_connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_runs (
                    run_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    job_json TEXT,
                    queue_state TEXT NOT NULL DEFAULT '',
                    worker_pid INTEGER,
                    active_process_pid INTEGER,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    heartbeat_at TEXT
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(skill_runs)").fetchall()
            }
            column_definitions = {
                "lease_owner": "TEXT",
                "lease_expires_at": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "max_attempts": f"INTEGER NOT NULL DEFAULT {self.queue_max_attempts}",
            }
            for column_name, definition in column_definitions.items():
                if column_name not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE skill_runs ADD COLUMN {column_name} {definition}"
                    )
        self.db_initialized = True

    def load_run_row(self, run_id: str) -> sqlite3.Row | None:
        self.ensure_initialized()
        with self.run_store_connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, record_json, job_json, queue_state, worker_pid,
                       active_process_pid, cancel_requested, created_at, updated_at,
                       heartbeat_at, lease_owner, lease_expires_at, attempt_count,
                       max_attempts
                FROM skill_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return row

    def inflate_run_record(self, row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(str(row["record_json"]))
        runtime = dict(record.get("runtime", {}))
        queue_state = str(row["queue_state"] or "").strip()
        if queue_state:
            runtime["queue_state"] = queue_state
        if not str(runtime.get("delivery", "")).strip() and row["job_json"] is not None:
            runtime["delivery"] = "durable_worker"
        runtime["store_backend"] = "sqlite"
        runtime["attempt_count"] = int(row["attempt_count"] or 0)
        runtime["max_attempts"] = int(row["max_attempts"] or self.queue_max_attempts)
        if row["lease_owner"] is not None:
            runtime["lease_owner"] = str(row["lease_owner"])
        if row["lease_expires_at"] is not None:
            runtime["lease_expires_at"] = str(row["lease_expires_at"])
        record["runtime"] = runtime
        record["cancel_requested"] = bool(row["cancel_requested"])
        return record

    def load_run_record(self, run_id: str) -> dict[str, Any] | None:
        row = self.load_run_row(run_id)
        if row is None:
            return None
        record = self.inflate_run_record(row)
        self.run_store[run_id] = record
        return record

    def upsert_run_row(
        self,
        *,
        run_id: str,
        record: dict[str, Any],
        job_context: dict[str, Any] | None = None,
        queue_state: str | None = None,
        worker_pid: int | None = None,
        active_process_pid: int | None = None,
        cancel_requested: bool | None = None,
        lease_owner: str | None = None,
        lease_expires_at: str | None = None,
        attempt_count: int | None = None,
        max_attempts: int | None = None,
        heartbeat: bool = False,
    ) -> None:
        self.ensure_initialized()
        existing = self.load_run_row(run_id)
        runtime = dict(record.get("runtime", {}))
        resolved_queue_state = (
            queue_state
            if queue_state is not None
            else str(runtime.get("queue_state", "")).strip()
            or (str(existing["queue_state"]) if existing is not None else "")
        )
        has_worker_delivery = bool(
            str(runtime.get("delivery", "")).strip()
            or job_context is not None
            or resolved_queue_state
            or (existing is not None and existing["job_json"] is not None)
        )
        if has_worker_delivery:
            runtime["delivery"] = "durable_worker"
        runtime["store_backend"] = "sqlite"
        if resolved_queue_state:
            runtime["queue_state"] = resolved_queue_state
        record["runtime"] = runtime

        resolved_cancel_requested = (
            bool(cancel_requested)
            if cancel_requested is not None
            else bool(record.get("cancel_requested", False))
            or (bool(existing["cancel_requested"]) if existing is not None else False)
        )
        record["cancel_requested"] = resolved_cancel_requested

        created_at = (
            str(existing["created_at"])
            if existing is not None and str(existing["created_at"]).strip()
            else self.utc_now()
        )
        updated_at = self.utc_now()
        heartbeat_at = (
            updated_at
            if heartbeat
            or resolved_queue_state
            in {"queued", "worker_starting", "executing", "cancelling"}
            else (
                str(existing["heartbeat_at"])
                if existing is not None and existing["heartbeat_at"] is not None
                else None
            )
        )
        resolved_worker_pid = (
            worker_pid
            if worker_pid is not None
            else int(existing["worker_pid"])
            if existing is not None and existing["worker_pid"] is not None
            else None
        )
        resolved_active_process_pid = (
            active_process_pid
            if active_process_pid is not None
            else int(existing["active_process_pid"])
            if existing is not None and existing["active_process_pid"] is not None
            else None
        )
        resolved_job_json = (
            json.dumps(job_context, ensure_ascii=False)
            if job_context is not None
            else str(existing["job_json"])
            if existing is not None and existing["job_json"] is not None
            else None
        )
        resolved_attempt_count = (
            int(attempt_count)
            if attempt_count is not None
            else int(runtime.get("attempt_count", 0))
            if runtime.get("attempt_count") is not None
            else int(existing["attempt_count"])
            if existing is not None and existing["attempt_count"] is not None
            else 0
        )
        resolved_max_attempts = (
            int(max_attempts)
            if max_attempts is not None
            else int(runtime.get("max_attempts", self.queue_max_attempts))
            if runtime.get("max_attempts") is not None
            else int(existing["max_attempts"])
            if existing is not None and existing["max_attempts"] is not None
            else self.queue_max_attempts
        )
        resolved_lease_owner = (
            lease_owner
            if lease_owner is not None
            else str(runtime.get("lease_owner"))
            if runtime.get("lease_owner") is not None
            else str(existing["lease_owner"])
            if existing is not None and existing["lease_owner"] is not None
            else None
        )
        resolved_lease_expires_at = (
            lease_expires_at
            if lease_expires_at is not None
            else str(runtime.get("lease_expires_at"))
            if runtime.get("lease_expires_at") is not None
            else str(existing["lease_expires_at"])
            if existing is not None and existing["lease_expires_at"] is not None
            else None
        )
        if resolved_queue_state in {
            "claimed",
            "worker_starting",
            "executing",
            "cancelling",
        }:
            if not resolved_lease_owner:
                resolved_lease_owner = self.lease_owner_id()
            if not resolved_lease_expires_at or heartbeat:
                resolved_lease_expires_at = self.lease_expires_at()
        elif resolved_queue_state in {
            "queued",
            "completed",
            "failed",
            "manual_required",
            "planned",
        }:
            resolved_lease_owner = None
            resolved_lease_expires_at = None
        runtime["attempt_count"] = resolved_attempt_count
        runtime["max_attempts"] = resolved_max_attempts
        if resolved_lease_owner:
            runtime["lease_owner"] = resolved_lease_owner
        else:
            runtime.pop("lease_owner", None)
        if resolved_lease_expires_at:
            runtime["lease_expires_at"] = resolved_lease_expires_at
        else:
            runtime.pop("lease_expires_at", None)
        record["runtime"] = runtime

        self.run_store[run_id] = dict(record)
        with self.run_store_connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_runs (
                    run_id, record_json, job_json, queue_state, worker_pid,
                    active_process_pid, cancel_requested, created_at, updated_at,
                    heartbeat_at, lease_owner, lease_expires_at, attempt_count,
                    max_attempts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    record_json = excluded.record_json,
                    job_json = excluded.job_json,
                    queue_state = excluded.queue_state,
                    worker_pid = excluded.worker_pid,
                    active_process_pid = excluded.active_process_pid,
                    cancel_requested = excluded.cancel_requested,
                    updated_at = excluded.updated_at,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    attempt_count = excluded.attempt_count,
                    max_attempts = excluded.max_attempts
                """,
                (
                    run_id,
                    json.dumps(record, ensure_ascii=False),
                    resolved_job_json,
                    resolved_queue_state,
                    resolved_worker_pid,
                    resolved_active_process_pid,
                    int(resolved_cancel_requested),
                    created_at,
                    updated_at,
                    heartbeat_at,
                    resolved_lease_owner,
                    resolved_lease_expires_at,
                    resolved_attempt_count,
                    resolved_max_attempts,
                ),
            )

    def load_run_job_context(self, run_id: str) -> dict[str, Any] | None:
        row = self.load_run_row(run_id)
        if row is None or row["job_json"] is None:
            return None
        return json.loads(str(row["job_json"]))

    def update_run_metadata(
        self,
        *,
        run_id: str,
        queue_state: str | None = None,
        worker_pid: int | None = None,
        active_process_pid: int | None = None,
        cancel_requested: bool | None = None,
        lease_owner: str | None = None,
        lease_expires_at: str | None = None,
        attempt_count: int | None = None,
        max_attempts: int | None = None,
        heartbeat: bool = False,
    ) -> None:
        record = self.load_run_record(run_id)
        if record is None:
            return
        self.upsert_run_row(
            run_id=run_id,
            record=record,
            queue_state=queue_state,
            worker_pid=worker_pid,
            active_process_pid=active_process_pid,
            cancel_requested=cancel_requested,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            heartbeat=heartbeat,
        )

    def is_cancel_requested(self, run_id: str) -> bool:
        row = self.load_run_row(run_id)
        return bool(row["cancel_requested"]) if row is not None else False

    def lease_owner_id(self) -> str:
        return f"pid:{os.getpid()}"

    def lease_expires_at(self) -> str:
        now_ts = datetime.now(timezone.utc).timestamp()
        return datetime.fromtimestamp(
            now_ts + self.queue_lease_timeout_s,
            tz=timezone.utc,
        ).isoformat()

    def is_lease_expired(self, value: str | None) -> bool:
        if not value or not str(value).strip():
            return False
        try:
            expires_at = datetime.fromisoformat(str(value))
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= expires_at
