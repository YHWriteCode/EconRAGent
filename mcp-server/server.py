from __future__ import annotations

import argparse
import asyncio
import json
import os
import py_compile
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from kg_agent.config import AgentLLMClient, AgentModelConfig, SkillRuntimeConfig
from kg_agent.skills.command_planner import (
    SkillCommandPlanner,
    build_shell_hints as build_skill_shell_hints,
    is_dry_run,
)
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillDefinition,
    SkillExecutionRequest,
    SkillFileEntry,
    SkillRunRecord,
    SkillRuntimeTarget,
)

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent
except ImportError:  # pragma: no cover - fallback for local tests without MCP package
    class TextContent:
        def __init__(self, *, type: str, text: str):
            self.type = type
            self.text = text

    class CallToolResult:
        def __init__(
            self,
            *,
            content: list[TextContent] | None = None,
            structuredContent: dict[str, Any] | None = None,
            isError: bool = False,
        ):
            self.content = list(content or [])
            self.structuredContent = structuredContent
            self.isError = isError

    class FastMCP:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def tool(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def resource(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def run(self) -> None:
            raise RuntimeError("mcp package is not installed")


SKILLS_ROOT = Path(os.environ.get("MCP_SKILLS_DIR", "/app/skills")).resolve()
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_DIR", "/workspace")).resolve()
RUN_STORE_DB_PATH = Path(
    os.environ.get(
        "MCP_RUN_STORE_SQLITE_PATH",
        str(WORKSPACE_ROOT / "skill_runtime_runs.sqlite3"),
    )
).resolve()
DEFAULT_SCRIPT_TIMEOUT_S = int(os.environ.get("MCP_SCRIPT_TIMEOUT_S", "120"))
DEFAULT_RUN_TIMEOUT_S = int(os.environ.get("MCP_RUN_TIMEOUT_S", "300"))
MAX_REFERENCE_BYTES = int(os.environ.get("MCP_MAX_REFERENCE_BYTES", "200000"))
MAX_LOG_PREVIEW_BYTES = int(os.environ.get("MCP_MAX_LOG_PREVIEW_BYTES", "12000"))
MAX_GENERATED_FILE_BYTES = 64000
MAX_REPAIR_ATTEMPTS = 1
WAIT_FOR_TERMINAL_GRACE_S = float(
    os.environ.get("MCP_WAIT_FOR_TERMINAL_GRACE_S", "10.0")
)
WORKER_TERMINAL_POLL_INTERVAL_S = float(
    os.environ.get("MCP_WORKER_TERMINAL_POLL_INTERVAL_S", "0.2")
)
WORKER_HEARTBEAT_TIMEOUT_S = float(
    os.environ.get("MCP_WORKER_HEARTBEAT_TIMEOUT_S", "30.0")
)
QUEUE_WORKER_POLL_INTERVAL_S = float(
    os.environ.get("MCP_QUEUE_WORKER_POLL_INTERVAL_S", "0.5")
)
QUEUE_WORKER_STARTUP_WAIT_S = float(
    os.environ.get("MCP_QUEUE_WORKER_STARTUP_WAIT_S", "1.5")
)

SKILL_RUNTIME_CONFIG = SkillRuntimeConfig.from_env()
DEFAULT_RUNTIME_TARGET = SkillRuntimeTarget.from_dict(
    SKILL_RUNTIME_CONFIG.default_runtime_target.to_dict(),
    default=SkillRuntimeTarget.linux_default(),
)


def _build_utility_llm_client() -> AgentLLMClient:
    config = AgentModelConfig.from_env_keys(
        provider_keys=(
            "KG_AGENT_UTILITY_MODEL_PROVIDER",
            "UTILITY_LLM_PROVIDER",
        ),
        model_keys=(
            "KG_AGENT_UTILITY_MODEL_NAME",
            "UTILITY_LLM_MODEL",
        ),
        base_url_keys=(
            "KG_AGENT_UTILITY_MODEL_BASE_URL",
            "UTILITY_LLM_BINDING_HOST",
        ),
        api_key_keys=(
            "KG_AGENT_UTILITY_MODEL_API_KEY",
            "UTILITY_LLM_BINDING_API_KEY",
        ),
        timeout_keys=(
            "KG_AGENT_UTILITY_MODEL_TIMEOUT_S",
            "UTILITY_LLM_TIMEOUT",
        ),
    )
    return AgentLLMClient(config)


UTILITY_LLM_CLIENT = _build_utility_llm_client()


class _SerializedUtilityLLMStub:
    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = [dict(item) for item in payloads]

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        if not self.payloads:
            raise RuntimeError("No serialized utility LLM payload remaining")
        return self.payloads.pop(0)

mcp = FastMCP("SkillRuntimeService", json_response=True)
RUN_STORE: dict[str, dict[str, Any]] = {}
LIVE_RUN_POLL_INTERVAL_S = float(
    os.environ.get("MCP_LIVE_RUN_POLL_INTERVAL_S", "0.2")
)
CANCEL_WAIT_TIMEOUT_S = float(os.environ.get("MCP_CANCEL_WAIT_TIMEOUT_S", "5.0"))
RUN_STORE_DB_INITIALIZED = False
QUEUE_WORKER_PROCESS: subprocess.Popen | None = None
IS_QUEUE_WORKER_PROCESS = os.environ.get("MCP_RUNTIME_ROLE", "").strip() == "queue_worker"


class SkillServerError(RuntimeError):
    pass


def _run_store_connect() -> sqlite3.Connection:
    RUN_STORE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(RUN_STORE_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_run_store_initialized() -> None:
    global RUN_STORE_DB_INITIALIZED
    if RUN_STORE_DB_INITIALIZED:
        return
    with _run_store_connect() as conn:
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
    RUN_STORE_DB_INITIALIZED = True


def _load_run_row(run_id: str) -> sqlite3.Row | None:
    _ensure_run_store_initialized()
    with _run_store_connect() as conn:
        row = conn.execute(
            """
            SELECT run_id, record_json, job_json, queue_state, worker_pid,
                   active_process_pid, cancel_requested, created_at, updated_at,
                   heartbeat_at
            FROM skill_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    return row


def _inflate_run_record(row: sqlite3.Row) -> dict[str, Any]:
    record = json.loads(str(row["record_json"]))
    runtime = dict(record.get("runtime", {}))
    queue_state = str(row["queue_state"] or "").strip()
    if queue_state:
        runtime["queue_state"] = queue_state
    if not str(runtime.get("delivery", "")).strip() and row["job_json"] is not None:
        runtime["delivery"] = "durable_worker"
    runtime["store_backend"] = "sqlite"
    record["runtime"] = runtime
    record["cancel_requested"] = bool(row["cancel_requested"])
    return record


def _load_run_record(run_id: str) -> dict[str, Any] | None:
    row = _load_run_row(run_id)
    if row is None:
        return None
    record = _inflate_run_record(row)
    RUN_STORE[run_id] = record
    return record


def _upsert_run_row(
    *,
    run_id: str,
    record: dict[str, Any],
    job_context: dict[str, Any] | None = None,
    queue_state: str | None = None,
    worker_pid: int | None = None,
    active_process_pid: int | None = None,
    cancel_requested: bool | None = None,
    heartbeat: bool = False,
) -> None:
    _ensure_run_store_initialized()
    existing = _load_run_row(run_id)
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
        else _utc_now()
    )
    updated_at = _utc_now()
    heartbeat_at = (
        updated_at
        if heartbeat or resolved_queue_state in {"queued", "worker_starting", "executing", "cancelling"}
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

    RUN_STORE[run_id] = dict(record)
    with _run_store_connect() as conn:
        conn.execute(
            """
            INSERT INTO skill_runs (
                run_id, record_json, job_json, queue_state, worker_pid,
                active_process_pid, cancel_requested, created_at, updated_at,
                heartbeat_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                record_json = excluded.record_json,
                job_json = excluded.job_json,
                queue_state = excluded.queue_state,
                worker_pid = excluded.worker_pid,
                active_process_pid = excluded.active_process_pid,
                cancel_requested = excluded.cancel_requested,
                updated_at = excluded.updated_at,
                heartbeat_at = excluded.heartbeat_at
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
            ),
        )


def _load_run_job_context(run_id: str) -> dict[str, Any] | None:
    row = _load_run_row(run_id)
    if row is None or row["job_json"] is None:
        return None
    return json.loads(str(row["job_json"]))


def _update_run_metadata(
    *,
    run_id: str,
    queue_state: str | None = None,
    worker_pid: int | None = None,
    active_process_pid: int | None = None,
    cancel_requested: bool | None = None,
    heartbeat: bool = False,
) -> None:
    record = _load_run_record(run_id)
    if record is None:
        return
    _upsert_run_row(
        run_id=run_id,
        record=record,
        queue_state=queue_state,
        worker_pid=worker_pid,
        active_process_pid=active_process_pid,
        cancel_requested=cancel_requested,
        heartbeat=heartbeat,
    )


def _is_cancel_requested(run_id: str) -> bool:
    row = _load_run_row(run_id)
    return bool(row["cancel_requested"]) if row is not None else False


def _is_process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int | None) -> None:
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


def _queue_worker_command() -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--queue-worker"]


def _spawn_queue_worker_process() -> subprocess.Popen:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["MCP_RUNTIME_ROLE"] = "queue_worker"
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(repo_root)
        if not existing_pythonpath
        else str(repo_root) + os.pathsep + existing_pythonpath
    )
    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
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
    return subprocess.Popen(_queue_worker_command(), **popen_kwargs)


def _ensure_queue_worker_process() -> int | None:
    global QUEUE_WORKER_PROCESS
    if IS_QUEUE_WORKER_PROCESS:
        return os.getpid()
    if QUEUE_WORKER_PROCESS is not None and QUEUE_WORKER_PROCESS.poll() is None:
        return int(QUEUE_WORKER_PROCESS.pid)
    QUEUE_WORKER_PROCESS = _spawn_queue_worker_process()
    return int(QUEUE_WORKER_PROCESS.pid)


def _recover_stale_queued_runs() -> None:
    _ensure_run_store_initialized()
    now = datetime.now(timezone.utc)
    with _run_store_connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, record_json, queue_state, worker_pid, active_process_pid, heartbeat_at
            FROM skill_runs
            WHERE queue_state IN ('worker_starting', 'claimed', 'executing', 'cancelling')
            """
        ).fetchall()
    for row in rows:
        queue_state = str(row["queue_state"] or "").strip()
        heartbeat_at = str(row["heartbeat_at"] or "").strip()
        if not heartbeat_at:
            continue
        try:
            heartbeat_ts = datetime.fromisoformat(heartbeat_at)
        except ValueError:
            continue
        age_s = (now - heartbeat_ts).total_seconds()
        if age_s < max(5.0, WORKER_HEARTBEAT_TIMEOUT_S):
            continue
        worker_pid = int(row["worker_pid"]) if row["worker_pid"] is not None else None
        active_process_pid = (
            int(row["active_process_pid"]) if row["active_process_pid"] is not None else None
        )
        worker_alive = _is_process_alive(worker_pid)
        active_alive = _is_process_alive(active_process_pid)
        record = json.loads(str(row["record_json"]))
        runtime = dict(record.get("runtime", {}))
        runtime["delivery"] = "durable_worker"
        runtime["store_backend"] = "sqlite"
        record["runtime"] = runtime
        if queue_state in {"worker_starting", "claimed"} and not worker_alive and not active_alive:
            runtime["queue_state"] = "queued"
            record["runtime"] = runtime
            record["summary"] = (
                f"Re-queued shell task for skill '{record.get('skill_name', '')}' after stale worker claim."
            )
            _upsert_run_row(
                run_id=str(row["run_id"]),
                record=record,
                queue_state="queued",
                worker_pid=None,
                active_process_pid=None,
                cancel_requested=bool(record.get("cancel_requested", False)),
                heartbeat=False,
            )
            continue
        if queue_state in {"executing", "cancelling"} and not worker_alive and not active_alive:
            record["run_status"] = "failed"
            record["status"] = "failed"
            record["success"] = False
            record["finished_at"] = record.get("finished_at") or _utc_now()
            record["failure_reason"] = "worker_lost"
            record["summary"] = f"Worker for run '{row['run_id']}' exited unexpectedly."
            runtime["queue_state"] = "failed"
            record["runtime"] = runtime
            _upsert_run_row(
                run_id=str(row["run_id"]),
                record=record,
                queue_state="failed",
                worker_pid=None,
                active_process_pid=None,
                cancel_requested=bool(record.get("cancel_requested", False)),
                heartbeat=True,
            )


def _claim_next_queued_run() -> str | None:
    _ensure_run_store_initialized()
    _recover_stale_queued_runs()
    now = _utc_now()
    claimed_run_id: str | None = None
    with _run_store_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT run_id
            FROM skill_runs
            WHERE queue_state = 'queued'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is not None:
            claimed_run_id = str(row["run_id"])
            conn.execute(
                """
                UPDATE skill_runs
                SET queue_state = ?, worker_pid = ?, heartbeat_at = ?, updated_at = ?
                WHERE run_id = ? AND queue_state = 'queued'
                """,
                ("claimed", os.getpid(), now, now, claimed_run_id),
            )
        conn.commit()
    return claimed_run_id


def _recover_stale_running_record(run_id: str) -> dict[str, Any] | None:
    _recover_stale_queued_runs()
    row = _load_run_row(run_id)
    if row is None:
        return None
    record = _inflate_run_record(row)
    if str(record.get("run_status", "")).strip() != "running":
        return record

    queue_state = str(row["queue_state"] or "").strip()
    if queue_state in {"queued", "claimed", "worker_starting"}:
        _ensure_queue_worker_process()
        return _load_run_record(run_id)

    heartbeat_at = str(row["heartbeat_at"] or "").strip()
    if not heartbeat_at:
        return record
    try:
        heartbeat_ts = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return record
    age_s = (datetime.now(timezone.utc) - heartbeat_ts).total_seconds()
    if age_s < max(5.0, WORKER_HEARTBEAT_TIMEOUT_S):
        return record
    worker_pid = int(row["worker_pid"]) if row["worker_pid"] is not None else None
    active_process_pid = (
        int(row["active_process_pid"]) if row["active_process_pid"] is not None else None
    )
    if _is_process_alive(worker_pid) or _is_process_alive(active_process_pid):
        return record

    record["run_status"] = "failed"
    record["status"] = "failed"
    record["success"] = False
    record["finished_at"] = record.get("finished_at") or _utc_now()
    record["failure_reason"] = "worker_lost"
    record["summary"] = f"Worker for run '{run_id}' exited unexpectedly."
    _upsert_run_row(
        run_id=run_id,
        record=record,
        queue_state="failed",
        worker_pid=None,
        active_process_pid=None,
        heartbeat=True,
    )
    return record


def _skill_dirs() -> list[Path]:
    if not SKILLS_ROOT.exists():
        return []
    return sorted(
        path
        for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def _resolve_skill_dir(skill_name: str) -> Path:
    if not skill_name or not skill_name.strip():
        raise SkillServerError("skill_name is required")
    requested = skill_name.strip()
    skill_dir = (SKILLS_ROOT / requested).resolve()
    if (
        skill_dir.parent == SKILLS_ROOT
        and skill_dir.is_dir()
        and (skill_dir / "SKILL.md").is_file()
    ):
        return skill_dir

    for candidate in _skill_dirs():
        metadata, body = _parse_skill_markdown(candidate / "SKILL.md")
        aliases = {
            candidate.name,
            str(metadata.get("name", "")).strip(),
            _extract_first_heading(body),
            str(candidate.resolve()),
        }
        aliases.discard("")
        if requested in aliases:
            return candidate
    raise SkillServerError(f"Unknown skill: {skill_name}")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_skill_markdown(skill_md_path: Path) -> tuple[dict[str, Any], str]:
    raw = _read_text(skill_md_path)
    metadata: dict[str, Any] = {}
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            frontmatter = raw[4:end]
            parsed = yaml.safe_load(frontmatter) or {}
            if isinstance(parsed, dict):
                metadata = parsed
            raw = raw[end + len("\n---\n") :]
    return metadata, raw.strip()


def _extract_first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _extract_summary(metadata: dict[str, Any], body: str) -> str:
    summary = metadata.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    paragraph: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    if paragraph:
        return " ".join(paragraph)
    return "No summary available."


def _extract_skill_tags(metadata: dict[str, Any]) -> list[str]:
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag).strip() for tag in tags if isinstance(tag, str) and tag.strip()]


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    return sorted(path for path in skill_dir.rglob("*") if path.is_file())


def _classify_skill_file(skill_dir: Path, path: Path) -> str:
    relative_path = str(path.relative_to(skill_dir)).replace("\\", "/")
    if relative_path == "SKILL.md":
        return "skill_doc"
    if relative_path.startswith("references/"):
        return "reference"
    if relative_path.startswith("scripts/"):
        return "script"
    if relative_path.startswith("assets/"):
        return "asset"
    if relative_path.endswith(".md"):
        return "markdown"
    return "other"


def _iter_runnable_scripts(skill_dir: Path) -> list[str]:
    runnable: list[str] = []
    for path in _iter_skill_files(skill_dir):
        relative_path = str(path.relative_to(skill_dir)).replace("\\", "/")
        if _classify_skill_file(skill_dir, path) != "script":
            continue
        suffix = path.suffix.lower()
        if suffix in {".py", ".sh", ".bash", ".ps1"} or os.access(path, os.X_OK):
            runnable.append(relative_path)
    return runnable


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _shell_join(argv: list[str]) -> str:
    normalized = [str(item) for item in argv if str(item)]
    if not normalized:
        return ""
    if os.name == "nt":
        return "& " + " ".join(_powershell_quote(item) for item in normalized)
    return shlex.join(normalized)


def _build_shell_exec_argv(shell_command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-Command", shell_command]
    return ["/bin/sh", "-lc", shell_command]


def _default_shell_command(relative_script: str) -> str:
    suffix = Path(relative_script).suffix.lower()
    quoted_script = shlex.quote(relative_script)
    if suffix == ".py":
        return f"python {quoted_script}"
    if suffix in {".sh", ".bash"}:
        return f"/bin/sh {quoted_script}"
    if suffix == ".ps1":
        return f"powershell -File {quoted_script}"
    return quoted_script


def _build_script_shell_command(
    *,
    skill_dir: Path,
    relative_script: str,
    cli_args: list[str] | None = None,
) -> str:
    script_path = (skill_dir / relative_script).resolve()
    suffix = script_path.suffix.lower()
    argv: list[str]
    if suffix == ".py":
        argv = [sys.executable, str(script_path)]
    elif suffix in {".sh", ".bash"}:
        shell_bin = "sh.exe" if os.name == "nt" else "/bin/sh"
        argv = [shell_bin, str(script_path)]
    elif suffix == ".ps1":
        shell_bin = "powershell.exe" if os.name == "nt" else "pwsh"
        argv = [shell_bin]
        if os.name == "nt":
            argv.append("-NoProfile")
        argv.extend(["-File", str(script_path)])
    else:
        argv = [str(script_path)]
    argv.extend(str(item) for item in (cli_args or []))
    return _shell_join(argv)


def _build_workspace_script_shell_command(
    *,
    workspace_dir: Path,
    relative_script: str,
    cli_args: list[str] | None = None,
) -> str:
    script_path = (workspace_dir / relative_script).resolve()
    suffix = script_path.suffix.lower()
    argv: list[str]
    if suffix == ".py":
        argv = [sys.executable, str(script_path)]
    elif suffix in {".sh", ".bash"}:
        shell_bin = "sh.exe" if os.name == "nt" else "/bin/sh"
        argv = [shell_bin, str(script_path)]
    elif suffix == ".ps1":
        shell_bin = "powershell.exe" if os.name == "nt" else "pwsh"
        argv = [shell_bin]
        if os.name == "nt":
            argv.append("-NoProfile")
        argv.extend(["-File", str(script_path)])
    else:
        argv = [str(script_path)]
    argv.extend(str(item) for item in (cli_args or []))
    return _shell_join(argv)


def _build_run_env(
    *,
    skill_dir: Path,
    workspace_dir: Path,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
    request_file: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    python_bin_dir = str(Path(sys.executable).resolve().parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = (
        python_bin_dir
        if not existing_path
        else python_bin_dir + os.pathsep + existing_path
    )
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "SKILL_NAME": skill_dir.name,
            "SKILL_ROOT": str(skill_dir),
            "SKILL_WORKSPACE": str(workspace_dir),
            "SKILL_GOAL": goal,
            "SKILL_USER_QUERY": user_query,
            "SKILL_CONSTRAINTS_JSON": json.dumps(constraints, ensure_ascii=False),
            "SKILL_REQUEST_FILE": str(request_file),
            "HOME": str(workspace_dir),
        }
    )
    return env


def _write_skill_request(
    *,
    workspace_dir: Path,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> Path:
    request_path = workspace_dir / "skill_request.json"
    request_path.write_text(
        json.dumps(
            {
                "skill_name": skill_payload["name"],
                "goal": goal,
                "user_query": user_query,
                "constraints": constraints,
                "skill": {
                    "name": skill_payload["name"],
                    "description": skill_payload["description"],
                    "tags": skill_payload["tags"],
                    "path": skill_payload["path"],
                },
                "shell_hints": skill_payload["shell_hints"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return request_path


def _collect_workspace_artifacts(workspace_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(workspace_dir.rglob("*")):
        if not path.is_file():
            continue
        artifacts.append(
            {
                "path": str(path.relative_to(workspace_dir)).replace("\\", "/"),
                "size_bytes": path.stat().st_size,
            }
        )
    return artifacts


def _truncate_log(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_LOG_PREVIEW_BYTES:
        return text, False
    return encoded[:MAX_LOG_PREVIEW_BYTES].decode("utf-8", errors="ignore"), True


def _build_skill_catalog_entry(skill_dir: Path) -> dict[str, Any]:
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    summary = _extract_summary(metadata, body)
    tags = _extract_skill_tags(metadata)
    shell_hints = build_skill_shell_hints(_build_loaded_skill_from_dir(skill_dir))
    return {
        "name": skill_dir.name,
        "description": summary,
        "tags": tags,
        "path": str(skill_dir),
        "execution_mode": "shell",
        "shell_example_count": len(shell_hints["example_commands"]),
        "runnable_script_count": len(shell_hints["runnable_scripts"]),
    }


def _score_skill_match(query: str, *, name: str, summary: str, tags: list[str]) -> int:
    query_lower = (query or "").strip().lower()
    if not query_lower:
        return 0
    parts = [name, summary, " ".join(tags)]
    search_text = " ".join(parts).replace("_", " ").replace("-", " ").lower()
    score = 0
    if name and name.lower() in query_lower:
        score += 5
    for token in re.findall(r"[a-z][a-z0-9_/-]{2,}", query_lower):
        normalized = token.strip("_-/")
        if normalized and normalized in search_text:
            score += 2
    for tag in tags:
        if len(tag) >= 3 and tag.lower() in query_lower:
            score += 3
    return score


def _iter_reference_files(skill_dir: Path) -> list[Path]:
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        return []
    return sorted(path for path in references_dir.rglob("*") if path.is_file())


def _load_skill_payload(skill_name: str) -> dict[str, Any]:
    skill_dir = _resolve_skill_dir(skill_name)
    raw_skill_md = _read_text(skill_dir / "SKILL.md")
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    summary = _extract_summary(metadata, body)
    references: list[dict[str, Any]] = []
    for ref_path in _iter_reference_files(skill_dir):
        content = _read_text(ref_path)
        byte_length = len(content.encode("utf-8"))
        truncated = False
        if byte_length > MAX_REFERENCE_BYTES:
            content = content.encode("utf-8")[:MAX_REFERENCE_BYTES].decode(
                "utf-8", errors="ignore"
            )
            truncated = True
        references.append(
            {
                "path": str(ref_path.relative_to(skill_dir)).replace("\\", "/"),
                "content": content,
                "truncated": truncated,
            }
        )

    scripts_dir = skill_dir / "scripts"
    scripts = []
    if scripts_dir.is_dir():
        scripts = sorted(
            str(path.relative_to(skill_dir)).replace("\\", "/")
            for path in scripts_dir.rglob("*")
            if path.is_file()
        )

    file_inventory = [
        {
            "path": str(path.relative_to(skill_dir)).replace("\\", "/"),
            "kind": _classify_skill_file(skill_dir, path),
            "size_bytes": path.stat().st_size,
        }
        for path in _iter_skill_files(skill_dir)
    ]
    shell_hints = build_skill_shell_hints(_build_loaded_skill_from_dir(skill_dir))

    return {
        "name": skill_dir.name,
        "summary": summary,
        "description": summary,
        "tags": _extract_skill_tags(metadata),
        "path": str(skill_dir),
        "metadata": metadata,
        "skill_md": raw_skill_md,
        "skill_body": body,
        "references": references,
        "scripts": scripts,
        "shell_hints": shell_hints,
        "file_inventory": file_inventory,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_loaded_skill(skill_name: str) -> LoadedSkill:
    skill_dir = _resolve_skill_dir(skill_name)
    return _build_loaded_skill_from_dir(skill_dir)


def _build_loaded_skill_from_dir(skill_dir: Path) -> LoadedSkill:
    raw_skill_md = _read_text(skill_dir / "SKILL.md")
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    name = (
        str(metadata.get("name", "")).strip()
        or _extract_first_heading(body)
        or skill_dir.name
    )
    file_inventory = [
        SkillFileEntry(
            path=str(path.relative_to(skill_dir)).replace("\\", "/"),
            kind=_classify_skill_file(skill_dir, path),
            size_bytes=path.stat().st_size,
        )
        for path in _iter_skill_files(skill_dir)
    ]
    return LoadedSkill(
        skill=SkillDefinition(
            name=name,
            description=_extract_summary(metadata, body),
            path=skill_dir.resolve(),
            tags=_extract_skill_tags(metadata),
            metadata=metadata,
        ),
        skill_md=raw_skill_md,
        file_inventory=file_inventory,
    )


def _build_skill_runtime_context(
    skill_name: str,
    goal: str,
    user_query: str,
    workspace: str | None,
    constraints: dict[str, Any],
) -> tuple[dict[str, Any], LoadedSkill, SkillExecutionRequest]:
    payload = _load_skill_payload(skill_name)
    loaded_skill = _build_loaded_skill(skill_name)
    request = SkillExecutionRequest(
        skill_name=skill_name,
        goal=goal,
        user_query=user_query,
        workspace=workspace,
        runtime_target=DEFAULT_RUNTIME_TARGET,
        constraints=constraints,
    )
    return payload, loaded_skill, request


async def _resolve_command_plan(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan_payload: dict[str, Any] | None,
) -> SkillCommandPlan:
    if isinstance(command_plan_payload, dict):
        return SkillCommandPlan.from_dict(
            command_plan_payload,
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            runtime_target=request.runtime_target,
            constraints=request.constraints,
        )
    planner = SkillCommandPlanner(
        llm_client=UTILITY_LLM_CLIENT,
        default_shell_mode=SKILL_RUNTIME_CONFIG.default_shell_mode,
        default_runtime_target=DEFAULT_RUNTIME_TARGET,
    )
    return await planner.plan(
        loaded_skill=loaded_skill,
        request=request,
    )


def _build_free_shell_repair_prompt(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan: SkillCommandPlan,
    command: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    preflight: dict[str, Any],
) -> tuple[str, str]:
    system_prompt = (
        "You repair failed free-shell skill execution plans. "
        "Return strict JSON only. "
        "Do not explain outside the JSON payload."
    )
    user_prompt = (
        "Return strict JSON only with this schema:\n"
        "{"
        '"mode": "free_shell" | "generated_script" | "manual_required", '
        '"command": str | null, '
        '"generated_files": [{"path": str, "content": str, "description": str}], '
        '"rationale": str, '
        '"missing_fields": [str], '
        '"failure_reason": str | null, '
        '"required_tools": [str], '
        '"warnings": [str]'
        "}\n\n"
        "Repair target runtime:\n"
        f"{json.dumps(command_plan.runtime_target.to_dict(), ensure_ascii=False, indent=2)}\n\n"
        "Original request:\n"
        f"{json.dumps({'skill_name': request.skill_name, 'goal': request.goal, 'user_query': request.user_query, 'constraints': request.constraints}, ensure_ascii=False, indent=2)}\n\n"
        "Previous command plan:\n"
        f"{json.dumps(command_plan.to_dict(), ensure_ascii=False, indent=2)}\n\n"
        f"Executed command:\n{command}\n\n"
        f"Exit code:\n{exit_code}\n\n"
        "Preflight result:\n"
        f"{json.dumps(preflight, ensure_ascii=False, indent=2)}\n\n"
        "stdout:\n"
        f"{stdout[-5000:]}\n\n"
        "stderr:\n"
        f"{stderr[-5000:]}\n\n"
        "Skill docs excerpt:\n"
        f"{loaded_skill.skill_md[:7000]}\n\n"
        "Rules:\n"
        "1. Prefer the minimal repair that fixes the observed failure.\n"
        "2. Preserve the declared runtime target.\n"
        "3. Keep generated file paths relative and do not use heredocs.\n"
        "4. If the failure cannot be repaired safely in one step, return manual_required.\n"
        "Keep JSON valid and do not include markdown fences."
    )
    return system_prompt, user_prompt


def _preflight_required_tools(command_plan: SkillCommandPlan) -> list[dict[str, Any]]:
    required_tools = command_plan.hints.get("required_tools", [])
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_tool in required_tools if isinstance(required_tools, list) else []:
        tool_name = str(raw_tool).strip()
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        resolved = shutil.which(tool_name)
        tools.append(
            {
                "name": tool_name,
                "available": bool(resolved),
                "resolved_path": resolved,
            }
        )
    return tools


def _validate_generated_file_specs(command_plan: SkillCommandPlan) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    workspace_root = Path(command_plan.runtime_target.workspace_root).resolve()
    for generated_file in command_plan.generated_files:
        relative_path = generated_file.path.replace("\\", "/").strip()
        encoded = generated_file.content.encode("utf-8")
        target_path = (workspace_root / relative_path).resolve()
        path_ok = target_path == workspace_root or workspace_root in target_path.parents
        size_ok = len(encoded) <= MAX_GENERATED_FILE_BYTES
        entries.append(
            {
                "path": relative_path,
                "size_bytes": len(encoded),
                "path_ok": path_ok,
                "size_ok": size_ok,
            }
        )
    return entries


def _run_preflight(
    *,
    workspace_dir: Path,
    command_plan: SkillCommandPlan,
) -> tuple[dict[str, Any], list[str]]:
    generated_file_specs = _validate_generated_file_specs(command_plan)
    required_tools = _preflight_required_tools(command_plan)
    written_paths: list[str] = []
    generated_file_results = [dict(item) for item in generated_file_specs]
    failure_reason: str | None = None
    status = "ok"

    for item in generated_file_results:
        if not item["path_ok"]:
            failure_reason = "generated_file_invalid_path"
            status = "manual_required"
            break
        if not item["size_ok"]:
            failure_reason = "generated_file_too_large"
            status = "manual_required"
            break

    if failure_reason is None:
        missing_tools = [item["name"] for item in required_tools if not item["available"]]
        if missing_tools:
            failure_reason = "missing_required_tools"
            status = "manual_required"

    if failure_reason is None:
        try:
            written_paths = _write_generated_files(
                workspace_dir=workspace_dir,
                command_plan=command_plan,
            )
        except Exception as exc:
            failure_reason = "generated_file_write_failed"
            status = "manual_required"
            generated_file_results.append(
                {
                    "path": "",
                    "path_ok": False,
                    "size_ok": False,
                    "write_error": str(exc),
                }
            )

    if failure_reason is None:
        for item in generated_file_results:
            path = str(item.get("path", "")).strip()
            if not path or not path.lower().endswith(".py"):
                continue
            if not command_plan.runtime_target.supports_python:
                item["python_compile"] = {
                    "checked": False,
                    "ok": False,
                    "error": "python_not_supported",
                }
                failure_reason = "python_not_supported"
                status = "manual_required"
                break
            target = (workspace_dir / path).resolve()
            try:
                py_compile.compile(str(target), doraise=True)
                item["python_compile"] = {"checked": True, "ok": True, "error": None}
            except py_compile.PyCompileError as exc:
                item["python_compile"] = {
                    "checked": True,
                    "ok": False,
                    "error": str(exc),
                }
                failure_reason = "generated_python_syntax_error"
                status = "manual_required"
                break

    preflight = {
        "status": status,
        "ok": failure_reason is None,
        "failure_reason": failure_reason,
        "required_tools": required_tools,
        "generated_files": generated_file_results,
    }
    return preflight, written_paths


async def _attempt_repair_plan(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan: SkillCommandPlan,
    command: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    preflight: dict[str, Any],
) -> SkillCommandPlan | None:
    if not UTILITY_LLM_CLIENT.is_available():
        return None
    system_prompt, user_prompt = _build_free_shell_repair_prompt(
        loaded_skill=loaded_skill,
        request=request,
        command_plan=command_plan,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        preflight=preflight,
    )
    payload = await UTILITY_LLM_CLIENT.complete_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.0,
        max_tokens=1800,
    )
    base_plan = SkillCommandPlan.from_dict(
        payload,
        skill_name=request.skill_name,
        goal=request.goal,
        user_query=request.user_query,
        runtime_target=command_plan.runtime_target,
        constraints=request.constraints,
    )
    required_tools = [
        str(item)
        for item in (
            payload.get("required_tools")
            if isinstance(payload.get("required_tools"), list)
            else []
        )
        if isinstance(item, (str, int, float))
    ]
    warnings = [
        str(item)
        for item in (
            payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        )
        if isinstance(item, (str, int, float))
    ]
    repaired_plan = SkillCommandPlan(
        skill_name=base_plan.skill_name,
        goal=base_plan.goal,
        user_query=base_plan.user_query,
        runtime_target=base_plan.runtime_target,
        constraints=dict(base_plan.constraints),
        command=base_plan.command,
        mode=base_plan.mode,
        shell_mode=base_plan.shell_mode,
        rationale=base_plan.rationale,
        entrypoint=base_plan.entrypoint,
        cli_args=list(base_plan.cli_args),
        generated_files=list(base_plan.generated_files),
        missing_fields=list(base_plan.missing_fields),
        failure_reason=base_plan.failure_reason,
        hints={
            **dict(command_plan.hints),
            "planner": "free_shell",
            "required_tools": required_tools,
            "warnings": warnings,
        },
    )
    if repaired_plan.is_manual_required or not repaired_plan.command:
        return None
    return repaired_plan


def _materialize_shell_command(
    *,
    loaded_skill: LoadedSkill,
    command_plan: SkillCommandPlan,
    workspace_dir: Path | None = None,
) -> str | None:
    if command_plan.mode == "explicit":
        return command_plan.command
    if command_plan.mode == "generated_script" and command_plan.generated_files:
        relative_script = command_plan.generated_files[0].path
        if workspace_dir is not None:
            return _build_workspace_script_shell_command(
                workspace_dir=workspace_dir,
                relative_script=relative_script,
                cli_args=command_plan.cli_args,
            )
        return command_plan.command or relative_script
    if command_plan.entrypoint:
        return _build_script_shell_command(
            skill_dir=loaded_skill.skill.path,
            relative_script=command_plan.entrypoint,
            cli_args=command_plan.cli_args,
        )
    return command_plan.command


def _write_generated_files(
    *,
    workspace_dir: Path,
    command_plan: SkillCommandPlan,
) -> list[str]:
    written_paths: list[str] = []
    for generated_file in command_plan.generated_files:
        relative_path = generated_file.path.replace("\\", "/").strip()
        if not relative_path:
            raise SkillServerError("Generated file path cannot be empty")
        target = (workspace_dir / relative_path).resolve()
        if target == workspace_dir or workspace_dir not in target.parents:
            raise SkillServerError("Generated files must stay inside the runtime workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated_file.content, encoding="utf-8")
        written_paths.append(str(target.relative_to(workspace_dir)).replace("\\", "/"))
    return written_paths


def _build_run_record_payload(
    *,
    record: SkillRunRecord,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
    payload: dict[str, Any],
    loaded_skill: LoadedSkill,
) -> dict[str, Any]:
    data = record.to_public_dict()
    data.update(
        {
            "name": payload["name"],
            "goal": goal,
            "user_query": user_query,
            "constraints": constraints,
            "skill": {
                "name": payload["name"],
                "description": payload["description"],
                "tags": payload["tags"],
                "path": payload["path"],
            },
            "file_inventory": payload["file_inventory"],
            "references": payload["references"],
            "shell_hints": build_skill_shell_hints(loaded_skill),
        }
    )
    return data


def _resolve_script_path(skill_dir: Path, script_name: str) -> Path:
    scripts_dir = (skill_dir / "scripts").resolve()
    if not scripts_dir.is_dir():
        raise SkillServerError(f"Skill has no scripts directory: {skill_dir.name}")
    script_path = (scripts_dir / script_name.strip()).resolve()
    if script_path == scripts_dir or scripts_dir not in script_path.parents:
        raise SkillServerError("script_name must resolve inside the skill scripts directory")
    if not script_path.is_file():
        raise SkillServerError(f"Unknown script: {script_name}")
    return script_path


def _resolve_skill_file_path(skill_dir: Path, relative_path: str) -> Path:
    if not relative_path or not relative_path.strip():
        raise SkillServerError("relative_path is required")
    target = (skill_dir / relative_path.strip()).resolve()
    if target != skill_dir and skill_dir not in target.parents:
        raise SkillServerError("relative_path must resolve inside the skill directory")
    if not target.is_file():
        raise SkillServerError(f"Unknown skill file: {relative_path}")
    return target


def _build_command(script_path: Path, args: list[str]) -> list[str]:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return ["python", str(script_path), *args]
    if suffix in {".sh", ".bash"}:
        return ["/bin/sh", str(script_path), *args]
    if os.access(script_path, os.X_OK):
        return [str(script_path), *args]
    raise SkillServerError(
        "Unsupported script type. Use a .py, .sh, .bash, or executable file."
    )


def _build_script_env(skill_dir: Path, workspace_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "SKILL_NAME": skill_dir.name,
            "SKILL_ROOT": str(skill_dir),
            "SKILL_WORKSPACE": str(workspace_dir),
            "HOME": str(workspace_dir),
        }
    )
    return env


def _store_run_record(record: dict[str, Any]) -> None:
    run_id = str(record.get("run_id", "")).strip()
    if run_id:
        _upsert_run_row(run_id=run_id, record=record)


def _update_live_run_record(
    *,
    run_id: str,
    workspace_dir: Path | None,
    stdout_text: str,
    stderr_text: str,
    summary: str | None = None,
    cancel_requested: bool | None = None,
    refresh_artifacts: bool = True,
) -> None:
    record = _load_run_record(run_id)
    if not isinstance(record, dict):
        return

    stdout_preview, stdout_truncated = _truncate_log(stdout_text)
    stderr_preview, stderr_truncated = _truncate_log(stderr_text)
    record["logs"] = {"stdout": stdout_text, "stderr": stderr_text}
    record["logs_preview"] = {
        "stdout": stdout_preview,
        "stderr": stderr_preview,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    if refresh_artifacts and isinstance(workspace_dir, Path) and workspace_dir.exists():
        record["artifacts"] = _collect_workspace_artifacts(workspace_dir)
    if summary is not None:
        record["summary"] = summary
    if cancel_requested is not None:
        record["cancel_requested"] = cancel_requested
        if cancel_requested:
            record["success"] = False
    runtime = dict(record.get("runtime", {}))
    runtime.setdefault("executor", "shell")
    runtime["delivery"] = "durable_worker"
    runtime["store_backend"] = "sqlite"
    runtime["cancel_supported"] = True
    runtime["log_streaming"] = True
    if cancel_requested is not None:
        runtime["cancel_requested"] = cancel_requested
    record["runtime"] = runtime
    _upsert_run_row(
        run_id=run_id,
        record=record,
        queue_state=str(runtime.get("queue_state", "")).strip() or None,
        cancel_requested=record.get("cancel_requested"),
        heartbeat=True,
    )


def _mark_run_cancel_requested(run_id: str) -> None:
    record = _load_run_record(run_id)
    if not isinstance(record, dict):
        return
    workspace = record.get("workspace")
    workspace_dir = (
        Path(workspace).resolve()
        if isinstance(workspace, str) and workspace.strip()
        else None
    )
    stdout = ""
    stderr = ""
    logs = record.get("logs")
    if isinstance(logs, dict):
        stdout = str(logs.get("stdout", ""))
        stderr = str(logs.get("stderr", ""))
    _update_live_run_record(
        run_id=run_id,
        workspace_dir=workspace_dir,
        stdout_text=stdout,
        stderr_text=stderr,
        summary=f"Cancellation requested for run '{run_id}'.",
        cancel_requested=True,
        refresh_artifacts=False,
    )
    _update_run_metadata(
        run_id=run_id,
        queue_state="cancelling",
        cancel_requested=True,
        heartbeat=True,
    )


def _resolve_wait_for_completion(
    *,
    constraints: dict[str, Any],
    wait_for_completion: bool | None,
) -> bool:
    if isinstance(wait_for_completion, bool):
        return wait_for_completion
    for key in ("wait_for_completion", "blocking", "sync"):
        if key in constraints:
            return bool(constraints.get(key))
    return False


def _store_attempt_snapshot(
    *,
    base_run_id: str,
    suffix: str,
    record: dict[str, Any],
) -> str:
    snapshot_run_id = f"{base_run_id}:{suffix}"
    snapshot = dict(record)
    snapshot["run_id"] = snapshot_run_id
    RUN_STORE[snapshot_run_id] = snapshot
    return snapshot_run_id


async def _execute_shell_command(
    *,
    run_id: str,
    shell_command: str,
    workspace_dir: Path,
    env: dict[str, str],
    timeout_s: int,
) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *_build_shell_exec_argv(shell_command),
        cwd=str(workspace_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    cancel_state = {"requested": _is_cancel_requested(run_id)}
    _update_run_metadata(
        run_id=run_id,
        queue_state="executing",
        worker_pid=os.getpid(),
        active_process_pid=process.pid,
        cancel_requested=cancel_state["requested"],
        heartbeat=True,
    )

    async def _consume_stream(
        stream: asyncio.StreamReader | None,
        chunks: list[str],
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="replace"))
            _update_live_run_record(
                run_id=run_id,
                workspace_dir=workspace_dir,
                stdout_text="".join(stdout_chunks),
                stderr_text="".join(stderr_chunks),
                cancel_requested=_is_cancel_requested(run_id),
                refresh_artifacts=False,
            )
            if _is_cancel_requested(run_id) and process.returncode is None:
                try:
                    cancel_state["requested"] = True
                    process.kill()
                except ProcessLookupError:
                    pass

    async def _poll_live_state() -> None:
        try:
            while process.returncode is None:
                cancel_state["requested"] = _is_cancel_requested(run_id)
                _update_live_run_record(
                    run_id=run_id,
                    workspace_dir=workspace_dir,
                    stdout_text="".join(stdout_chunks),
                    stderr_text="".join(stderr_chunks),
                    cancel_requested=cancel_state["requested"],
                    refresh_artifacts=True,
                )
                _update_run_metadata(
                    run_id=run_id,
                    queue_state="cancelling" if cancel_state["requested"] else "executing",
                    worker_pid=os.getpid(),
                    active_process_pid=process.pid,
                    cancel_requested=cancel_state["requested"],
                    heartbeat=True,
                )
                if cancel_state["requested"] and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.sleep(max(0.05, LIVE_RUN_POLL_INTERVAL_S))
        except asyncio.CancelledError:
            return

    stdout_task = asyncio.create_task(_consume_stream(process.stdout, stdout_chunks))
    stderr_task = asyncio.create_task(_consume_stream(process.stderr, stderr_chunks))
    poll_task = asyncio.create_task(_poll_live_state())
    timed_out = False
    try:
        if _is_cancel_requested(run_id) and process.returncode is None:
            try:
                cancel_state["requested"] = True
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), timeout=max(1, int(timeout_s)))
    except asyncio.TimeoutError:
        timed_out = True
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass

    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)
    stdout_preview, stdout_truncated = _truncate_log(stdout_text)
    stderr_preview, stderr_truncated = _truncate_log(stderr_text)
    cancel_requested = bool(cancel_state["requested"] or _is_cancel_requested(run_id))
    exit_code = process.returncode
    if cancel_requested and not timed_out and exit_code in {None, 0}:
        exit_code = 130
    elif timed_out and exit_code in {None, 0}:
        exit_code = 124
    _update_live_run_record(
        run_id=run_id,
        workspace_dir=workspace_dir,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        cancel_requested=cancel_requested,
        refresh_artifacts=True,
    )
    _update_run_metadata(
        run_id=run_id,
        queue_state="failed" if (cancel_requested or timed_out or exit_code != 0) else "completed",
        worker_pid=os.getpid(),
        active_process_pid=None,
        cancel_requested=cancel_requested,
        heartbeat=True,
    )
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cancel_requested": cancel_requested,
        "cancelled": cancel_requested and not timed_out,
        "success": (exit_code == 0) and not timed_out and not cancel_requested,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "logs_preview": {
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        },
        "artifacts": _collect_workspace_artifacts(workspace_dir),
    }


async def _complete_shell_run(
    *,
    skill_payload: dict[str, Any],
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan: SkillCommandPlan,
    run_id: str,
    started_at: str,
    workspace_dir: Path,
    request_file: Path,
    generated_files: list[str],
    preflight: dict[str, Any],
    shell_command: str,
    env: dict[str, str],
    timeout_s: int,
    cleanup_workspace: bool,
) -> dict[str, Any]:
    try:
        _update_run_metadata(
            run_id=run_id,
            queue_state="executing",
            worker_pid=os.getpid(),
            cancel_requested=_is_cancel_requested(run_id),
            heartbeat=True,
        )
        execution = await _execute_shell_command(
            run_id=run_id,
            shell_command=shell_command,
            workspace_dir=workspace_dir,
            env=env,
            timeout_s=timeout_s,
        )

        final_plan = command_plan
        final_preflight = preflight
        final_command = shell_command
        final_execution = execution
        repair_attempted = False
        repair_succeeded = False
        repaired_from_run_id: str | None = None

        can_repair = (
            not execution["success"]
            and not execution.get("cancelled")
            and command_plan.shell_mode == "free_shell"
            and command_plan.mode in {"free_shell", "generated_script"}
        )
        if can_repair:
            repair_attempted = True
            failed_attempt_record = SkillRunRecord(
                skill_name=request.skill_name,
                run_status="failed",
                success=False,
                summary=f"Initial shell execution for skill '{request.skill_name}' failed.",
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=_utc_now(),
                exit_code=execution["exit_code"],
                failure_reason=("timed_out" if execution["timed_out"] else "process_failed"),
                artifacts=execution["artifacts"],
                logs_preview=execution["logs_preview"],
                runtime={"executor": "shell", "delivery": "durable_worker"},
                preflight=preflight,
                repair_attempted=True,
            )
            failed_attempt_payload = _build_run_record_payload(
                record=failed_attempt_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            failed_attempt_payload["request_file"] = str(request_file)
            failed_attempt_payload["generated_files"] = generated_files
            failed_attempt_payload["logs"] = {
                "stdout": execution["stdout"],
                "stderr": execution["stderr"],
            }
            repaired_from_run_id = _store_attempt_snapshot(
                base_run_id=run_id,
                suffix="attempt-1",
                record=failed_attempt_payload,
            )

            repaired_plan = None
            try:
                for _attempt in range(MAX_REPAIR_ATTEMPTS):
                    repaired_plan = await _attempt_repair_plan(
                        loaded_skill=loaded_skill,
                        request=request,
                        command_plan=command_plan,
                        command=shell_command,
                        exit_code=execution["exit_code"],
                        stdout=execution["stdout"],
                        stderr=execution["stderr"],
                        preflight=preflight,
                    )
                    if repaired_plan is not None:
                        break
            except Exception:
                repaired_plan = None

            if repaired_plan is not None:
                repaired_preflight, repaired_generated_files = _run_preflight(
                    workspace_dir=workspace_dir,
                    command_plan=repaired_plan,
                )
                repaired_command = _materialize_shell_command(
                    loaded_skill=loaded_skill,
                    command_plan=repaired_plan,
                    workspace_dir=workspace_dir,
                )
                if repaired_preflight["ok"] and repaired_command:
                    repaired_execution = await _execute_shell_command(
                        run_id=run_id,
                        shell_command=repaired_command,
                        workspace_dir=workspace_dir,
                        env=env,
                        timeout_s=timeout_s,
                    )
                    final_plan = repaired_plan
                    final_preflight = repaired_preflight
                    final_command = repaired_command
                    final_execution = repaired_execution
                    generated_files = repaired_generated_files
                    repair_succeeded = repaired_execution["success"]
                else:
                    final_plan = repaired_plan
                    final_preflight = repaired_preflight
                    final_command = repaired_command or final_command
                    final_execution = {
                        "exit_code": execution["exit_code"],
                        "timed_out": False,
                        "success": False,
                        "stdout": execution["stdout"],
                        "stderr": execution["stderr"],
                        "logs_preview": execution["logs_preview"],
                        "artifacts": _collect_workspace_artifacts(workspace_dir),
                    }

        final_success = bool(final_execution["success"])
        final_run_status = "completed" if final_success else "failed"
        final_failure_reason = None
        if not final_success:
            if final_preflight.get("failure_reason"):
                final_failure_reason = final_preflight["failure_reason"]
            elif final_execution.get("cancelled"):
                final_failure_reason = "cancelled"
            elif final_execution.get("timed_out"):
                final_failure_reason = "timed_out"
            else:
                final_failure_reason = "process_failed"

        final_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status=final_run_status,
            success=final_success,
            summary=(
                f"Executed shell command for skill '{request.skill_name}'."
                if final_success
                else (
                    f"Shell execution for skill '{request.skill_name}' was cancelled."
                    if final_execution.get("cancelled")
                    else f"Shell execution for skill '{request.skill_name}' failed."
                )
            ),
            command_plan=final_plan,
            run_id=run_id,
            command=final_command,
            workspace=str(workspace_dir),
            started_at=started_at,
            finished_at=_utc_now(),
            exit_code=final_execution["exit_code"],
            failure_reason=final_failure_reason,
            artifacts=final_execution["artifacts"],
            logs_preview=final_execution["logs_preview"],
            runtime={
                "executor": "shell",
                "delivery": "durable_worker",
                "cancel_supported": True,
                "log_streaming": True,
            },
            preflight=final_preflight,
            repair_attempted=repair_attempted,
            repair_succeeded=repair_succeeded,
            repaired_from_run_id=repaired_from_run_id,
            cancel_requested=bool(final_execution.get("cancel_requested", False)),
        )
        response = _build_run_record_payload(
            record=final_record,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
            payload=skill_payload,
            loaded_skill=loaded_skill,
        )
        response["request_file"] = str(request_file)
        response["generated_files"] = generated_files
        response["timed_out"] = bool(final_execution.get("timed_out", False))
        response["logs"] = {
            "stdout": final_execution["stdout"],
            "stderr": final_execution["stderr"],
        }
        _store_run_record(response)
        _update_run_metadata(
            run_id=run_id,
            queue_state=final_run_status,
            worker_pid=os.getpid(),
            active_process_pid=None,
            cancel_requested=bool(final_execution.get("cancel_requested", False)),
            heartbeat=True,
        )
        return response
    except Exception as exc:
        failed_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status="failed",
            success=False,
            summary=f"Skill runtime crashed while executing '{request.skill_name}'.",
            command_plan=command_plan,
            run_id=run_id,
            command=shell_command,
            workspace=str(workspace_dir),
            started_at=started_at,
            finished_at=_utc_now(),
            failure_reason="runtime_internal_error",
            artifacts=_collect_workspace_artifacts(workspace_dir),
            runtime={
                "executor": "shell",
                "delivery": "durable_worker",
                "cancel_supported": True,
                "log_streaming": True,
                "error": str(exc),
            },
            preflight=preflight,
            cancel_requested=_is_cancel_requested(run_id),
        )
        response = _build_run_record_payload(
            record=failed_record,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
            payload=skill_payload,
            loaded_skill=loaded_skill,
        )
        response["request_file"] = str(request_file)
        response["generated_files"] = generated_files
        response["logs"] = {"stdout": "", "stderr": str(exc)}
        _store_run_record(response)
        _update_run_metadata(
            run_id=run_id,
            queue_state="failed",
            worker_pid=os.getpid(),
            active_process_pid=None,
            cancel_requested=_is_cancel_requested(run_id),
            heartbeat=True,
        )
        return response
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_dir, ignore_errors=True)


def _build_worker_job_context(
    *,
    request: SkillExecutionRequest,
    command_plan: SkillCommandPlan,
    run_id: str,
    started_at: str,
    workspace_dir: Path,
    request_file: Path,
    generated_files: list[str],
    preflight: dict[str, Any],
    shell_command: str,
    timeout_s: int,
    cleanup_workspace: bool,
) -> dict[str, Any]:
    utility_llm_stub_payloads: list[dict[str, Any]] = []
    raw_stub_payloads = getattr(UTILITY_LLM_CLIENT, "payloads", None)
    if isinstance(raw_stub_payloads, list):
        utility_llm_stub_payloads = [
            dict(item) for item in raw_stub_payloads if isinstance(item, dict)
        ]
    return {
        "skill_name": request.skill_name,
        "goal": request.goal,
        "user_query": request.user_query,
        "workspace": request.workspace,
        "constraints": dict(request.constraints),
        "runtime_target": request.runtime_target.to_dict(),
        "command_plan": command_plan.to_dict(),
        "run_id": run_id,
        "started_at": started_at,
        "workspace_dir": str(workspace_dir),
        "request_file": str(request_file),
        "generated_files": list(generated_files),
        "preflight": dict(preflight),
        "shell_command": shell_command,
        "timeout_s": int(timeout_s),
        "cleanup_workspace": bool(cleanup_workspace),
        "utility_llm_stub_payloads": utility_llm_stub_payloads,
    }

async def _wait_for_terminal_run_record(
    *,
    run_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    _ensure_queue_worker_process()
    deadline = asyncio.get_running_loop().time() + max(1.0, timeout_s)
    while True:
        record = _recover_stale_running_record(run_id)
        if record is None:
            raise SkillServerError(f"Unknown run_id: {run_id}")
        if str(record.get("run_status", "")).strip() != "running":
            return record
        if asyncio.get_running_loop().time() >= deadline:
            return record
        await asyncio.sleep(max(0.05, WORKER_TERMINAL_POLL_INTERVAL_S))


async def _run_shell_task(
    *,
    skill_payload: dict[str, Any],
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan: SkillCommandPlan,
    timeout_s: int,
    cleanup_workspace: bool,
    wait_for_completion: bool,
) -> dict[str, Any]:
    workspace_dir = Path(
        tempfile.mkdtemp(prefix=f"{skill_payload['name']}-", dir=str(WORKSPACE_ROOT))
    ).resolve()
    run_id = f"skill-run-{uuid.uuid4().hex}"
    started_at = _utc_now()
    request_file: Path | None = None

    cleanup_workspace_here = cleanup_workspace
    try:
        preflight, generated_files = _run_preflight(
            workspace_dir=workspace_dir,
            command_plan=command_plan,
        )
        shell_command = _materialize_shell_command(
            loaded_skill=loaded_skill,
            command_plan=command_plan,
            workspace_dir=workspace_dir,
        )
        if not shell_command:
            raise SkillServerError("Command plan did not resolve to an executable shell command")

        request_file = _write_skill_request(
            workspace_dir=workspace_dir,
            skill_payload=skill_payload,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
        )

        if not preflight["ok"]:
            preflight_record = SkillRunRecord(
                skill_name=request.skill_name,
                run_status="manual_required",
                success=False,
                summary=(
                    f"Preflight failed for skill '{request.skill_name}' before execution."
                ),
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=_utc_now(),
                failure_reason=preflight.get("failure_reason"),
                runtime={"executor": "shell"},
                preflight=preflight,
            )
            response = _build_run_record_payload(
                record=preflight_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            response["request_file"] = str(request_file)
            response["generated_files"] = generated_files
            response["logs"] = {"stdout": "", "stderr": ""}
            _store_run_record(response)
            return response

        running_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status="running",
            success=True,
            summary=f"Running shell task for skill '{request.skill_name}'.",
            command_plan=command_plan,
            run_id=run_id,
            command=shell_command,
            workspace=str(workspace_dir),
            started_at=started_at,
            runtime={
                "executor": "shell",
                "delivery": "durable_worker",
                "queue_state": "queued",
                "store_backend": "sqlite",
                "cancel_supported": True,
                "log_streaming": True,
            },
            preflight=preflight,
            cancel_requested=False,
        )
        running_payload = _build_run_record_payload(
            record=running_record,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
            payload=skill_payload,
            loaded_skill=loaded_skill,
        )
        running_payload["request_file"] = str(request_file)
        running_payload["generated_files"] = generated_files
        running_payload["logs"] = {"stdout": "", "stderr": ""}
        _store_run_record(running_payload)
        job_context = _build_worker_job_context(
            request=request,
            command_plan=command_plan,
            run_id=run_id,
            started_at=started_at,
            workspace_dir=workspace_dir,
            request_file=request_file,
            generated_files=generated_files,
            preflight=preflight,
            shell_command=shell_command,
            timeout_s=timeout_s,
            cleanup_workspace=cleanup_workspace,
        )
        _upsert_run_row(
            run_id=run_id,
            record=running_payload,
            job_context=job_context,
            queue_state="queued",
            cancel_requested=False,
            heartbeat=False,
        )
        cleanup_workspace_here = False
        try:
            worker_pid = _ensure_queue_worker_process()
        except Exception as exc:
            failed_record = SkillRunRecord(
                skill_name=request.skill_name,
                run_status="failed",
                success=False,
                summary=f"Failed to start durable worker for skill '{request.skill_name}'.",
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=_utc_now(),
                failure_reason="worker_start_failed",
                runtime={
                    "executor": "shell",
                    "delivery": "durable_worker",
                    "queue_state": "failed",
                    "store_backend": "sqlite",
                    "cancel_supported": True,
                    "log_streaming": True,
                    "error": str(exc),
                },
                preflight=preflight,
            )
            failed_payload = _build_run_record_payload(
                record=failed_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            failed_payload["request_file"] = str(request_file)
            failed_payload["generated_files"] = generated_files
            failed_payload["logs"] = {"stdout": "", "stderr": ""}
            _store_run_record(failed_payload)
            _update_run_metadata(
                run_id=run_id,
                queue_state="failed",
                worker_pid=None,
                active_process_pid=None,
                cancel_requested=False,
                heartbeat=True,
            )
            return failed_payload
        if wait_for_completion:
            return await _wait_for_terminal_run_record(
                run_id=run_id,
                timeout_s=float(timeout_s) + WAIT_FOR_TERMINAL_GRACE_S,
            )
        running_payload["notes"] = (
            "Shell task was enqueued for a durable worker. Poll get_run_status/get_run_logs/"
            "get_run_artifacts with run_id for live progress or terminal results. "
            "Use cancel_skill_run to stop it."
        )
        running_payload["runtime"] = dict(running_payload.get("runtime", {}))
        running_payload["runtime"]["queue_state"] = "queued"
        if worker_pid is not None:
            running_payload["runtime"]["queue_worker_pid"] = worker_pid
        return running_payload
    finally:
        if cleanup_workspace_here:
            shutil.rmtree(workspace_dir, ignore_errors=True)


async def _run_durable_worker(run_id: str) -> int:
    global UTILITY_LLM_CLIENT
    job_context = _load_run_job_context(run_id)
    if not isinstance(job_context, dict):
        raise SkillServerError(f"Missing worker job context for run_id: {run_id}")

    current_record = _load_run_record(run_id)
    if current_record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    if str(current_record.get("run_status", "")).strip() != "running":
        return 0

    skill_name = str(job_context.get("skill_name", "")).strip()
    goal = str(job_context.get("goal", "")).strip()
    user_query = str(job_context.get("user_query", "")).strip()
    workspace = (
        str(job_context.get("workspace")).strip()
        if isinstance(job_context.get("workspace"), str)
        and str(job_context.get("workspace")).strip()
        else None
    )
    constraints = (
        dict(job_context.get("constraints"))
        if isinstance(job_context.get("constraints"), dict)
        else {}
    )
    runtime_target = SkillRuntimeTarget.from_dict(job_context.get("runtime_target"))
    command_plan = SkillCommandPlan.from_dict(
        job_context.get("command_plan"),
        skill_name=skill_name,
        goal=goal,
        user_query=user_query,
        runtime_target=runtime_target,
        constraints=constraints,
    )
    started_at = str(job_context.get("started_at") or _utc_now())
    workspace_dir = Path(str(job_context.get("workspace_dir", ""))).resolve()
    request_file = Path(str(job_context.get("request_file", ""))).resolve()
    generated_files = [
        str(item)
        for item in (
            job_context.get("generated_files")
            if isinstance(job_context.get("generated_files"), list)
            else []
        )
        if isinstance(item, (str, int, float))
    ]
    preflight = (
        dict(job_context.get("preflight"))
        if isinstance(job_context.get("preflight"), dict)
        else {}
    )
    shell_command = str(job_context.get("shell_command", "")).strip()
    timeout_s = int(job_context.get("timeout_s", DEFAULT_RUN_TIMEOUT_S))
    cleanup_workspace = bool(job_context.get("cleanup_workspace", False))
    stub_payloads = (
        job_context.get("utility_llm_stub_payloads")
        if isinstance(job_context.get("utility_llm_stub_payloads"), list)
        else []
    )
    if stub_payloads:
        UTILITY_LLM_CLIENT = _SerializedUtilityLLMStub(
            [dict(item) for item in stub_payloads if isinstance(item, dict)]
        )

    payload = _load_skill_payload(skill_name)
    loaded_skill = _build_loaded_skill(skill_name)
    request = SkillExecutionRequest(
        skill_name=skill_name,
        goal=goal,
        user_query=user_query,
        workspace=workspace,
        runtime_target=runtime_target,
        constraints=constraints,
    )

    if _is_cancel_requested(run_id):
        cancelled_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status="failed",
            success=False,
            summary=f"Shell execution for skill '{request.skill_name}' was cancelled before start.",
            command_plan=command_plan,
            run_id=run_id,
            command=shell_command,
            workspace=str(workspace_dir),
            started_at=started_at,
            finished_at=_utc_now(),
            exit_code=130,
            failure_reason="cancelled",
            artifacts=_collect_workspace_artifacts(workspace_dir),
            runtime={
                "executor": "shell",
                "delivery": "durable_worker",
                "queue_state": "failed",
                "store_backend": "sqlite",
                "cancel_supported": True,
                "log_streaming": True,
            },
            preflight=preflight,
            cancel_requested=True,
        )
        cancelled_payload = _build_run_record_payload(
            record=cancelled_record,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
            payload=payload,
            loaded_skill=loaded_skill,
        )
        cancelled_payload["request_file"] = str(request_file)
        cancelled_payload["generated_files"] = generated_files
        cancelled_payload["logs"] = {"stdout": "", "stderr": ""}
        _store_run_record(cancelled_payload)
        _update_run_metadata(
            run_id=run_id,
            queue_state="failed",
            worker_pid=os.getpid(),
            active_process_pid=None,
            cancel_requested=True,
            heartbeat=True,
        )
        if cleanup_workspace:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        return 0

    env = _build_run_env(
        skill_dir=Path(payload["path"]).resolve(),
        workspace_dir=workspace_dir,
        goal=request.goal,
        user_query=request.user_query,
        constraints=request.constraints,
        request_file=request_file,
    )
    await _complete_shell_run(
        skill_payload=payload,
        loaded_skill=loaded_skill,
        request=request,
        command_plan=command_plan,
        run_id=run_id,
        started_at=started_at,
        workspace_dir=workspace_dir,
        request_file=request_file,
        generated_files=generated_files,
        preflight=preflight,
        shell_command=shell_command,
        env=env,
        timeout_s=timeout_s,
        cleanup_workspace=cleanup_workspace,
    )
    return 0


async def _run_queue_worker_loop() -> int:
    idle_started_at = datetime.now(timezone.utc)
    while True:
        claimed_run_id = _claim_next_queued_run()
        if not claimed_run_id:
            idle_age_s = (datetime.now(timezone.utc) - idle_started_at).total_seconds()
            if idle_age_s >= max(2.0, QUEUE_WORKER_STARTUP_WAIT_S * 2):
                return 0
            await asyncio.sleep(max(0.1, QUEUE_WORKER_POLL_INTERVAL_S))
            continue
        idle_started_at = datetime.now(timezone.utc)
        try:
            await _run_durable_worker(claimed_run_id)
        except Exception as exc:
            record = _load_run_record(claimed_run_id)
            if record is not None and str(record.get("run_status", "")).strip() == "running":
                record["run_status"] = "failed"
                record["status"] = "failed"
                record["success"] = False
                record["finished_at"] = record.get("finished_at") or _utc_now()
                record["failure_reason"] = "runtime_internal_error"
                record["summary"] = (
                    f"Queue worker crashed while executing run '{claimed_run_id}'."
                )
                runtime = dict(record.get("runtime", {}))
                runtime["delivery"] = "durable_worker"
                runtime["store_backend"] = "sqlite"
                runtime["queue_state"] = "failed"
                runtime["error"] = str(exc)
                record["runtime"] = runtime
                _upsert_run_row(
                    run_id=claimed_run_id,
                    record=record,
                    queue_state="failed",
                    worker_pid=os.getpid(),
                    active_process_pid=None,
                    cancel_requested=bool(record.get("cancel_requested", False)),
                    heartbeat=True,
                )


@mcp.tool()
def list_skills(query: str | None = None) -> dict[str, Any]:
    """Return the lightweight local skill catalog."""
    skills = [_build_skill_catalog_entry(skill_dir) for skill_dir in _skill_dirs()]
    selected_skill = None
    if query:
        ranked = sorted(
            (
                (
                    _score_skill_match(
                        query,
                        name=item["name"],
                        summary=item["description"],
                        tags=item["tags"],
                    ),
                    item,
                )
                for item in skills
            ),
            key=lambda item: (item[0], item[1]["name"]),
            reverse=True,
        )
        if ranked and ranked[0][0] > 0:
            selected_skill = ranked[0][1]
    return {
        "summary": f"Discovered {len(skills)} skill(s)",
        "skills": skills,
        "count": len(skills),
        "query": query or "",
        "selected_skill": selected_skill,
        "selected_skill_name": (
            selected_skill["name"] if isinstance(selected_skill, dict) else None
        ),
    }


@mcp.tool()
def read_skill(skill_name: str) -> dict[str, Any]:
    """Load SKILL.md and the indexed file inventory for one skill."""
    payload = _load_skill_payload(skill_name)
    return {
        "summary": f"Loaded skill '{payload['name']}'",
        "name": payload["name"],
        "description": payload["description"],
        "tags": payload["tags"],
        "path": payload["path"],
        "skill": {
            "name": payload["name"],
            "description": payload["description"],
            "tags": payload["tags"],
            "path": payload["path"],
        },
        "skill_md": payload["skill_md"],
        "file_inventory": payload["file_inventory"],
        "references": payload["references"],
        "shell_hints": payload["shell_hints"],
        "execution_mode": "shell",
    }


@mcp.tool()
def read_skill_file(skill_name: str, relative_path: str) -> dict[str, Any]:
    """Read one file from a skill directory by relative path."""
    skill_dir = _resolve_skill_dir(skill_name)
    target = _resolve_skill_file_path(skill_dir, relative_path)
    content = _read_text(target)
    byte_length = len(content.encode("utf-8"))
    truncated = False
    if byte_length > MAX_REFERENCE_BYTES:
        content = content.encode("utf-8")[:MAX_REFERENCE_BYTES].decode(
            "utf-8", errors="ignore"
        )
        truncated = True
    return {
        "summary": f"Read skill file '{relative_path}' from '{skill_name}'",
        "skill_name": skill_name,
        "path": str(target.relative_to(skill_dir)).replace("\\", "/"),
        "kind": _classify_skill_file(skill_dir, target),
        "content": content,
        "truncated": truncated,
    }


@mcp.tool()
def read_skill_docs(skill_name: str) -> dict[str, Any]:
    """Compatibility wrapper around the new coarse-grained read_skill interface."""
    payload = read_skill(skill_name)
    payload["summary"] = f"Loaded docs for skill '{skill_name}'"
    return payload


@mcp.tool()
async def run_skill_task(
    skill_name: str,
    goal: str,
    user_query: str | None = None,
    workspace: str | None = None,
    constraints: dict[str, Any] | None = None,
    command_plan: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_RUN_TIMEOUT_S,
    cleanup_workspace: bool = False,
    wait_for_completion: bool | None = None,
) -> dict[str, Any]:
    """Execute a skill task through a shell-oriented runtime boundary."""
    normalized_constraints = constraints or {}
    payload, loaded_skill, request = _build_skill_runtime_context(
        skill_name=skill_name,
        goal=goal,
        user_query=user_query or "",
        workspace=workspace,
        constraints=normalized_constraints,
    )
    resolved_command_plan = await _resolve_command_plan(
        loaded_skill=loaded_skill,
        request=request,
        command_plan_payload=command_plan,
    )
    resolved_wait_for_completion = _resolve_wait_for_completion(
        constraints=normalized_constraints,
        wait_for_completion=wait_for_completion,
    )

    if resolved_command_plan.is_manual_required:
        now = _utc_now()
        run_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status="manual_required",
            success=False,
            summary=f"Skill '{skill_name}' needs more execution detail before it can run.",
            command_plan=resolved_command_plan,
            run_id=f"skill-run-{uuid.uuid4().hex}",
            command=None,
            workspace=None,
            started_at=now,
            finished_at=now,
            failure_reason=resolved_command_plan.failure_reason,
            runtime={"executor": "shell"},
        )
        response = _build_run_record_payload(
            record=run_record,
            goal=goal,
            user_query=user_query or "",
            constraints=normalized_constraints,
            payload=payload,
            loaded_skill=loaded_skill,
        )
        response["notes"] = (
            "Pass constraints.shell_command (or constraints.command), or provide structured "
            "CLI args in constraints.args / constraints.cli_args when the skill has a single "
            "runnable entrypoint."
        )
        response["logs"] = {"stdout": "", "stderr": ""}
        _store_run_record(response)
        return response

    materialized_command = _materialize_shell_command(
        loaded_skill=loaded_skill,
        command_plan=resolved_command_plan,
    )
    if is_dry_run(normalized_constraints):
        now = _utc_now()
        run_record = SkillRunRecord(
            skill_name=request.skill_name,
            run_status="planned",
            success=True,
            summary=f"Planned shell task for '{skill_name}' without execution.",
            command_plan=resolved_command_plan,
            run_id=f"skill-run-{uuid.uuid4().hex}",
            command=materialized_command,
            workspace=None,
            started_at=now,
            finished_at=now,
            runtime={"executor": "shell"},
        )
        response = _build_run_record_payload(
            record=run_record,
            goal=goal,
            user_query=user_query or "",
            constraints=normalized_constraints,
            payload=payload,
            loaded_skill=loaded_skill,
        )
        response["notes"] = "Dry run only; the shell command was planned but not executed."
        response["logs"] = {"stdout": "", "stderr": ""}
        _store_run_record(response)
        return response

    return await _run_shell_task(
        skill_payload=payload,
        loaded_skill=loaded_skill,
        request=request,
        command_plan=resolved_command_plan,
        timeout_s=timeout_s,
        cleanup_workspace=cleanup_workspace,
        wait_for_completion=resolved_wait_for_completion,
    )


@mcp.tool()
def get_run_status(run_id: str) -> dict[str, Any]:
    """Fetch lifecycle status for a prior shell-based skill run."""
    record = _recover_stale_running_record(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    return {
        "summary": record.get("summary"),
        "run_id": run_id,
        "skill_name": record.get("skill_name"),
        "run_status": record.get("run_status"),
        "status": record.get("status"),
        "success": bool(record.get("success")),
        "command": record.get("command"),
        "shell_mode": record.get("shell_mode"),
        "runtime_target": record.get("runtime_target", {}),
        "workspace": record.get("workspace"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "exit_code": record.get("exit_code"),
        "failure_reason": record.get("failure_reason"),
        "command_plan": record.get("command_plan", {}),
        "runtime": record.get("runtime", {}),
        "execution_mode": record.get("execution_mode", "shell"),
        "preflight": record.get("preflight", {}),
        "repair_attempted": bool(record.get("repair_attempted", False)),
        "repair_succeeded": bool(record.get("repair_succeeded", False)),
        "repaired_from_run_id": record.get("repaired_from_run_id"),
        "cancel_requested": bool(record.get("cancel_requested", False)),
    }


@mcp.tool()
async def cancel_skill_run(
    run_id: str,
    wait_for_termination: bool = True,
) -> dict[str, Any]:
    """Request cancellation for a background shell skill run."""
    record = _recover_stale_running_record(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    if str(record.get("run_status", "")).strip() != "running":
        return get_run_status(run_id)
    _mark_run_cancel_requested(run_id)
    row = _load_run_row(run_id)
    if row is not None and row["active_process_pid"] is not None:
        _terminate_pid(int(row["active_process_pid"]))

    if wait_for_termination:
        terminal = await _wait_for_terminal_run_record(
            run_id=run_id,
            timeout_s=max(0.5, CANCEL_WAIT_TIMEOUT_S),
        )
        if str(terminal.get("run_status", "")).strip() != "running":
            return get_run_status(run_id)
    return get_run_status(run_id)


@mcp.tool()
def get_run_logs(run_id: str) -> dict[str, Any]:
    """Fetch full stdout/stderr logs for a prior shell-based skill run."""
    record = _recover_stale_running_record(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    logs = record.get("logs", {})
    return {
        "summary": f"Loaded logs for run '{run_id}'",
        "run_id": run_id,
        "skill_name": record.get("skill_name"),
        "run_status": record.get("run_status"),
        "status": record.get("status"),
        "success": bool(record.get("success")),
        "command": record.get("command"),
        "shell_mode": record.get("shell_mode"),
        "runtime_target": record.get("runtime_target", {}),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "failure_reason": record.get("failure_reason"),
        "runtime": record.get("runtime", {}),
        "preflight": record.get("preflight", {}),
        "repair_attempted": bool(record.get("repair_attempted", False)),
        "repair_succeeded": bool(record.get("repair_succeeded", False)),
        "repaired_from_run_id": record.get("repaired_from_run_id"),
        "cancel_requested": bool(record.get("cancel_requested", False)),
        "stdout": logs.get("stdout", ""),
        "stderr": logs.get("stderr", ""),
    }


@mcp.tool()
def get_run_artifacts(run_id: str) -> dict[str, Any]:
    """List artifacts produced by a prior shell-based skill run."""
    record = _recover_stale_running_record(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    return {
        "summary": f"Loaded artifacts for run '{run_id}'",
        "run_id": run_id,
        "skill_name": record.get("skill_name"),
        "run_status": record.get("run_status"),
        "status": record.get("status"),
        "success": bool(record.get("success")),
        "shell_mode": record.get("shell_mode"),
        "runtime_target": record.get("runtime_target", {}),
        "workspace": record.get("workspace"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "failure_reason": record.get("failure_reason"),
        "runtime": record.get("runtime", {}),
        "preflight": record.get("preflight", {}),
        "repair_attempted": bool(record.get("repair_attempted", False)),
        "repair_succeeded": bool(record.get("repair_succeeded", False)),
        "repaired_from_run_id": record.get("repaired_from_run_id"),
        "cancel_requested": bool(record.get("cancel_requested", False)),
        "artifacts": record.get("artifacts", []),
    }


@mcp.tool()
async def execute_skill_script(
    skill_name: str,
    script_name: str,
    args: list[str] | None = None,
    timeout_s: int = DEFAULT_SCRIPT_TIMEOUT_S,
    cleanup_workspace: bool = False,
) -> CallToolResult:
    """Legacy compatibility wrapper for explicit script execution."""
    skill_dir = _resolve_skill_dir(skill_name)
    script_path = _resolve_script_path(skill_dir, script_name)
    workspace_dir = Path(
        tempfile.mkdtemp(prefix=f"{skill_dir.name}-", dir=str(WORKSPACE_ROOT))
    ).resolve()
    command = _build_command(script_path, [str(item) for item in (args or [])])

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace_dir),
            env=_build_script_env(skill_dir, workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, int(timeout_s)),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SkillServerError(
                f"Script timed out after {timeout_s} seconds: {script_name}"
            ) from exc

        produced_files = sorted(
            str(path.relative_to(workspace_dir)).replace("\\", "/")
            for path in workspace_dir.rglob("*")
            if path.is_file()
        )
        result_payload = {
            "skill_name": skill_dir.name,
            "script_name": str(script_path.relative_to(skill_dir)).replace("\\", "/"),
            "command": command,
            "workspace": str(workspace_dir),
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "produced_files": produced_files,
            "success": process.returncode == 0,
            "summary": (
                f"Executed {skill_dir.name}/{script_path.name} "
                f"with exit code {process.returncode}"
            ),
        }
        text = result_payload["summary"]
        if result_payload["stderr"]:
            text = f"{text}\n\nstderr:\n{result_payload['stderr']}"
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=result_payload,
            isError=process.returncode != 0,
        )
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_dir, ignore_errors=True)


@mcp.resource("skill://catalog")
def skill_catalog_resource() -> str:
    """Read-only skill catalog resource for clients that support resources/list/read."""
    return json.dumps(list_skills(), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}")
def skill_resource(skill_name: str) -> str:
    """Read-only resource exposing one skill's coarse-grained payload."""
    return json.dumps(read_skill(skill_name), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}/docs")
def skill_docs_resource(skill_name: str) -> str:
    """Compatibility resource exposing one skill's docs payload."""
    return json.dumps(read_skill_docs(skill_name), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}/files/{relative_path}")
def skill_file_resource(skill_name: str, relative_path: str) -> str:
    """Read one skill file by relative path."""
    return json.dumps(
        read_skill_file(skill_name, relative_path),
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("skill://{skill_name}/references/{reference_name}")
def skill_reference_resource(skill_name: str, reference_name: str) -> str:
    """Read one reference file by relative path."""
    skill_dir = _resolve_skill_dir(skill_name)
    references_dir = (skill_dir / "references").resolve()
    if not references_dir.is_dir():
        raise SkillServerError(f"Skill has no references directory: {skill_name}")
    ref_path = (references_dir / reference_name).resolve()
    if ref_path == references_dir or references_dir not in ref_path.parents:
        raise SkillServerError("reference_name must resolve inside references/")
    if not ref_path.is_file():
        raise SkillServerError(f"Unknown reference: {reference_name}")
    return _read_text(ref_path)


@mcp.resource("skill-run://{run_id}/logs")
def skill_run_logs_resource(run_id: str) -> str:
    """Read full logs for a completed shell-based skill run."""
    return json.dumps(get_run_logs(run_id), ensure_ascii=False, indent=2)


@mcp.resource("skill-run://{run_id}/artifacts")
def skill_run_artifacts_resource(run_id: str) -> str:
    """Read artifact metadata for a completed shell-based skill run."""
    return json.dumps(get_run_artifacts(run_id), ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-run-id", default="")
    parser.add_argument("--queue-worker", action="store_true")
    args, _unknown = parser.parse_known_args()

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    _ensure_run_store_initialized()
    if bool(args.queue_worker):
        raise SystemExit(asyncio.run(_run_queue_worker_loop()))
    if str(args.worker_run_id).strip():
        raise SystemExit(asyncio.run(_run_durable_worker(str(args.worker_run_id).strip())))
    _ensure_queue_worker_process()
    mcp.run()


if __name__ == "__main__":
    main()
