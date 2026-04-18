from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass
class QueueRuntimeDeps:
    repo_root: Path
    entrypoint_path: Path
    queue_worker_concurrency: int
    queue_max_attempts: int
    worker_heartbeat_timeout_s: float
    utc_now: Callable[[], str]
    lease_owner_id: Callable[[], str]
    lease_expires_at: Callable[[], str]
    run_store_connect: Callable[[], sqlite3.Connection]
    ensure_run_store_initialized: Callable[[], None]
    load_run_row: Callable[[str], sqlite3.Row | None]
    inflate_run_record: Callable[[sqlite3.Row], dict[str, Any]]
    load_run_record: Callable[[str], dict[str, Any] | None]
    upsert_run_row: Callable[..., None]
    is_lease_expired: Callable[[str | None], bool]
    load_terminal_snapshot: Callable[..., dict[str, Any] | None]
    call_ensure_queue_worker_processes: Callable[[], list[int]]
    call_spawn_queue_worker_process: Callable[[], subprocess.Popen]


class QueueRuntimeManager:
    def __init__(self, deps: QueueRuntimeDeps) -> None:
        self.deps = deps
        self.worker_processes: dict[int, subprocess.Popen] = {}
        self.is_queue_worker_process = (
            os.environ.get("MCP_RUNTIME_ROLE", "").strip() == "queue_worker"
        )

    @staticmethod
    def is_process_alive(pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def terminate_pid(pid: int | None) -> None:
        if pid is None or pid <= 0:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        try:
            os.kill(pid, 15)
        except OSError:
            return

    def queue_worker_command(self) -> list[str]:
        return [sys.executable, str(self.deps.entrypoint_path), "--queue-worker"]

    def spawn_queue_worker_process(self) -> subprocess.Popen:
        env = os.environ.copy()
        env["MCP_RUNTIME_ROLE"] = "queue_worker"
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            str(self.deps.repo_root)
            if not existing_pythonpath
            else str(self.deps.repo_root) + os.pathsep + existing_pythonpath
        )
        popen_kwargs: dict[str, Any] = {
            "cwd": str(self.deps.repo_root),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        return subprocess.Popen(self.queue_worker_command(), **popen_kwargs)

    def reap_queue_worker_processes(self) -> None:
        stale_pids = [
            pid
            for pid, process in self.worker_processes.items()
            if process.poll() is not None
        ]
        for pid in stale_pids:
            self.worker_processes.pop(pid, None)

    def ensure_queue_worker_processes(self) -> list[int]:
        if self.is_queue_worker_process:
            return [os.getpid()]
        self.reap_queue_worker_processes()
        while len(self.worker_processes) < max(1, self.deps.queue_worker_concurrency):
            process = self.deps.call_spawn_queue_worker_process()
            self.worker_processes[int(process.pid)] = process
        return sorted(self.worker_processes)

    def maybe_recover_terminal_snapshot(
        self,
        *,
        run_id: str,
        row: sqlite3.Row | None = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        resolved_row = row or self.deps.load_run_row(run_id)
        if resolved_row is None:
            return None
        resolved_record = (
            dict(record)
            if isinstance(record, dict)
            else self.deps.inflate_run_record(resolved_row)
        )
        workspace = str(resolved_record.get("workspace", "")).strip()
        if not workspace:
            return None
        snapshot_record = self.deps.load_terminal_snapshot(
            workspace_dir=Path(workspace).resolve(),
            run_id=run_id,
        )
        if snapshot_record is None:
            return None
        recovered_record = dict(snapshot_record)
        runtime = dict(recovered_record.get("runtime", {}))
        runtime["terminal_snapshot_recovered"] = True
        recovered_record["runtime"] = runtime
        terminal_queue_state = (
            str(recovered_record.get("run_status", "")).strip() or "failed"
        )
        self.deps.upsert_run_row(
            run_id=run_id,
            record=recovered_record,
            queue_state=terminal_queue_state,
            worker_pid=None,
            active_process_pid=None,
            cancel_requested=bool(recovered_record.get("cancel_requested", False)),
            lease_owner=None,
            lease_expires_at=None,
            attempt_count=int(resolved_row["attempt_count"] or 0),
            max_attempts=int(resolved_row["max_attempts"] or self.deps.queue_max_attempts),
            heartbeat=True,
        )
        return self.deps.load_run_record(run_id)

    def recover_stale_queued_runs(self) -> None:
        self.deps.ensure_run_store_initialized()
        now = datetime.now(timezone.utc)
        with self.deps.run_store_connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, record_json, queue_state, worker_pid, active_process_pid,
                       heartbeat_at, lease_owner, lease_expires_at, attempt_count,
                       max_attempts
                FROM skill_runs
                WHERE queue_state IN ('worker_starting', 'claimed', 'executing', 'cancelling')
                """
            ).fetchall()
        for row in rows:
            record = json.loads(str(row["record_json"]))
            recovered_record = self.maybe_recover_terminal_snapshot(
                run_id=str(row["run_id"]),
                row=row,
                record=record,
            )
            if recovered_record is not None:
                continue
            queue_state = str(row["queue_state"] or "").strip()
            heartbeat_at = str(row["heartbeat_at"] or "").strip()
            age_s: float | None = None
            if heartbeat_at:
                try:
                    heartbeat_ts = datetime.fromisoformat(heartbeat_at)
                    age_s = (now - heartbeat_ts).total_seconds()
                except ValueError:
                    age_s = None
            lease_expired = self.deps.is_lease_expired(
                str(row["lease_expires_at"] or "").strip() or None
            )
            if (
                age_s is None or age_s < max(5.0, self.deps.worker_heartbeat_timeout_s)
            ) and not lease_expired:
                continue
            worker_pid = int(row["worker_pid"]) if row["worker_pid"] is not None else None
            active_process_pid = (
                int(row["active_process_pid"])
                if row["active_process_pid"] is not None
                else None
            )
            worker_alive = self.is_process_alive(worker_pid)
            active_alive = self.is_process_alive(active_process_pid)
            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = int(row["max_attempts"] or self.deps.queue_max_attempts)
            runtime = dict(record.get("runtime", {}))
            runtime["delivery"] = "durable_worker"
            runtime["store_backend"] = "sqlite"
            runtime["attempt_count"] = attempt_count
            runtime["max_attempts"] = max_attempts
            record["runtime"] = runtime
            should_requeue_claim = queue_state in {"worker_starting", "claimed"} and (
                lease_expired or (not worker_alive and not active_alive)
            )
            if should_requeue_claim:
                runtime["queue_state"] = "queued"
                runtime.pop("lease_owner", None)
                runtime.pop("lease_expires_at", None)
                record["runtime"] = runtime
                record["summary"] = (
                    f"Re-queued shell task for skill '{record.get('skill_name', '')}' "
                    "after stale worker claim."
                )
                self.deps.upsert_run_row(
                    run_id=str(row["run_id"]),
                    record=record,
                    queue_state="queued",
                    worker_pid=None,
                    active_process_pid=None,
                    cancel_requested=bool(record.get("cancel_requested", False)),
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    heartbeat=False,
                )
                continue
            should_recover_execution = queue_state in {"executing", "cancelling"} and (
                not active_alive and (lease_expired or not worker_alive)
            )
            if should_recover_execution:
                if attempt_count < max_attempts:
                    runtime["queue_state"] = "queued"
                    runtime.pop("lease_owner", None)
                    runtime.pop("lease_expires_at", None)
                    record["runtime"] = runtime
                    record["summary"] = (
                        f"Re-queued shell task for skill '{record.get('skill_name', '')}' "
                        "after worker loss."
                    )
                    self.deps.upsert_run_row(
                        run_id=str(row["run_id"]),
                        record=record,
                        queue_state="queued",
                        worker_pid=None,
                        active_process_pid=None,
                        cancel_requested=bool(record.get("cancel_requested", False)),
                        lease_owner=None,
                        lease_expires_at=None,
                        attempt_count=attempt_count,
                        max_attempts=max_attempts,
                        heartbeat=False,
                    )
                    continue
                record["run_status"] = "failed"
                record["status"] = "failed"
                record["success"] = False
                record["finished_at"] = record.get("finished_at") or self.deps.utc_now()
                record["failure_reason"] = "worker_lost"
                record["summary"] = f"Worker for run '{row['run_id']}' exited unexpectedly."
                runtime["queue_state"] = "failed"
                runtime.pop("lease_owner", None)
                runtime.pop("lease_expires_at", None)
                record["runtime"] = runtime
                self.deps.upsert_run_row(
                    run_id=str(row["run_id"]),
                    record=record,
                    queue_state="failed",
                    worker_pid=None,
                    active_process_pid=None,
                    cancel_requested=bool(record.get("cancel_requested", False)),
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    heartbeat=True,
                )

    def claim_next_queued_run(self) -> str | None:
        self.deps.ensure_run_store_initialized()
        self.recover_stale_queued_runs()
        now = self.deps.utc_now()
        claimed_run_id: str | None = None
        next_attempt_count = 0
        max_attempts = self.deps.queue_max_attempts
        with self.deps.run_store_connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT run_id, attempt_count, max_attempts
                FROM skill_runs
                WHERE queue_state = 'queued' AND attempt_count < max_attempts
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                claimed_run_id = str(row["run_id"])
                next_attempt_count = int(row["attempt_count"] or 0) + 1
                max_attempts = int(row["max_attempts"] or self.deps.queue_max_attempts)
                conn.execute(
                    """
                    UPDATE skill_runs
                    SET queue_state = ?, worker_pid = ?, heartbeat_at = ?, updated_at = ?,
                        lease_owner = ?, lease_expires_at = ?, attempt_count = ?, max_attempts = ?
                    WHERE run_id = ? AND queue_state = 'queued'
                    """,
                    (
                        "claimed",
                        os.getpid(),
                        now,
                        now,
                        self.deps.lease_owner_id(),
                        self.deps.lease_expires_at(),
                        next_attempt_count,
                        max_attempts,
                        claimed_run_id,
                    ),
                )
            conn.commit()
        if claimed_run_id is not None:
            record = self.deps.load_run_record(claimed_run_id)
            if isinstance(record, dict):
                self.deps.upsert_run_row(
                    run_id=claimed_run_id,
                    record=record,
                    queue_state="claimed",
                    worker_pid=os.getpid(),
                    active_process_pid=None,
                    cancel_requested=bool(record.get("cancel_requested", False)),
                    attempt_count=next_attempt_count,
                    max_attempts=max_attempts,
                    heartbeat=True,
                )
        return claimed_run_id

    def recover_stale_running_record(self, run_id: str) -> dict[str, Any] | None:
        self.recover_stale_queued_runs()
        row = self.deps.load_run_row(run_id)
        if row is None:
            return None
        record = self.deps.inflate_run_record(row)
        if str(record.get("run_status", "")).strip() != "running":
            return record

        recovered_record = self.maybe_recover_terminal_snapshot(
            run_id=run_id,
            row=row,
            record=record,
        )
        if recovered_record is not None:
            return recovered_record

        queue_state = str(row["queue_state"] or "").strip()
        if queue_state in {"queued", "claimed", "worker_starting"}:
            self.deps.call_ensure_queue_worker_processes()
            return self.deps.load_run_record(run_id)

        heartbeat_at = str(row["heartbeat_at"] or "").strip()
        if not heartbeat_at:
            return record
        try:
            heartbeat_ts = datetime.fromisoformat(heartbeat_at)
        except ValueError:
            return record
        age_s = (datetime.now(timezone.utc) - heartbeat_ts).total_seconds()
        if age_s < max(5.0, self.deps.worker_heartbeat_timeout_s):
            return record
        worker_pid = int(row["worker_pid"]) if row["worker_pid"] is not None else None
        active_process_pid = (
            int(row["active_process_pid"])
            if row["active_process_pid"] is not None
            else None
        )
        if self.is_process_alive(worker_pid) or self.is_process_alive(active_process_pid):
            return record

        record["run_status"] = "failed"
        record["status"] = "failed"
        record["success"] = False
        record["finished_at"] = record.get("finished_at") or self.deps.utc_now()
        record["failure_reason"] = "worker_lost"
        record["summary"] = f"Worker for run '{run_id}' exited unexpectedly."
        self.deps.upsert_run_row(
            run_id=run_id,
            record=record,
            queue_state="failed",
            worker_pid=None,
            active_process_pid=None,
            heartbeat=True,
        )
        return record
