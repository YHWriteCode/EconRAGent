from __future__ import annotations

import asyncio
import importlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

for _module_name in (
    "mcp_server_runtime.config",
    "mcp_server_runtime.errors",
    "mcp_server_runtime.utils",
    "mcp_server_runtime.skills",
    "mcp_server_runtime.workspace",
    "mcp_server_runtime.envs",
    "mcp_server_runtime.planning",
    "mcp_server_runtime.store",
    "mcp_server_runtime.queue",
    "mcp_server_runtime.execution",
    "mcp_server_runtime.service",
    "mcp_server_runtime.transport",
    "mcp_server_runtime.cli",
):
    _loaded_module = sys.modules.get(_module_name)
    if _loaded_module is not None:
        importlib.reload(_loaded_module)

from kg_agent.skills.command_planner import is_dry_run
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillRunRecord,
    SkillRuntimeTarget,
)
from mcp_server_runtime import config as runtime_config
from mcp_server_runtime.cli import RuntimeCliDeps, run_main
from mcp_server_runtime.config import (
    DEFAULT_RUN_TIMEOUT_S,
    DEFAULT_SCRIPT_TIMEOUT_S,
    ENVS_ROOT,
    ENV_HASH_FORMAT_VERSION,
    LOCKS_ROOT,
    MAX_BOOTSTRAP_ATTEMPTS,
    MAX_REFERENCE_BYTES,
    MAX_REPAIR_ATTEMPTS,
    PIP_CACHE_ROOT,
    QUEUE_LEASE_TIMEOUT_S,
    QUEUE_MAX_ATTEMPTS,
    QUEUE_WORKER_CONCURRENCY,
    QUEUE_WORKER_POLL_INTERVAL_S,
    QUEUE_WORKER_STARTUP_WAIT_S,
    RUNS_ROOT,
    RUN_STORE_DB_PATH,
    SKILLS_ROOT,
    STATE_ROOT,
    WAIT_FOR_TERMINAL_GRACE_S,
    WHEELHOUSE_ROOT,
    WORKER_HEARTBEAT_TIMEOUT_S,
    WORKER_TERMINAL_POLL_INTERVAL_S,
    WORKSPACE_ROOT,
    _SerializedUtilityLLMStub,
)
from mcp_server_runtime.envs import (
    _build_run_env,
    _build_script_env,
    _build_skill_env_spec,
    _prefetch_skill_wheels_for_skill,
    _skill_env_metadata_path,
    _skill_env_runtime_payload,
)
from mcp_server_runtime.errors import SkillServerError
from mcp_server_runtime.planning import (
    _attempt_repair_plan,
    _build_run_record_payload,
    _build_skill_runtime_context,
    _materialize_shell_command,
    _request_with_runtime_target,
    _resolve_command_plan,
    _run_preflight,
)
from mcp_server_runtime.execution import RuntimeExecutionDeps, RuntimeExecutionManager
from mcp_server_runtime.queue import QueueRuntimeDeps, QueueRuntimeManager
from mcp_server_runtime.service import RuntimeService, RuntimeServiceDeps
from mcp_server_runtime.skills import (
    _build_command,
    _build_loaded_skill,
    _build_loaded_skill_from_dir,
    _build_skill_catalog_entry,
    _classify_skill_file,
    _load_skill_payload,
    _read_text,
    _resolve_script_path,
    _resolve_skill_dir,
    _resolve_skill_file_path,
    _score_skill_match,
    _skill_dirs,
)
from mcp_server_runtime.transport import build_transport_bindings
from mcp_server_runtime.utils import (
    build_shell_exec_argv as _build_shell_exec_argv,
    utc_duration_seconds as _utc_duration_seconds,
    utc_now as _utc_now,
)
from mcp_server_runtime.store import RuntimeRunStore
from mcp_server_runtime.workspace import (
    _collect_workspace_artifacts,
    _load_terminal_snapshot,
    _materialize_skill_workspace_view,
    _prepare_transport_payload,
    _truncate_log,
    _write_json_atomic,
    _write_mirrored_skill_manifest,
    _write_skill_request,
    _write_terminal_snapshot,
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

mcp = FastMCP("SkillRuntimeService", json_response=True)
DEFAULT_RUNTIME_TARGET = runtime_config.DEFAULT_RUNTIME_TARGET
UTILITY_LLM_CLIENT = runtime_config.UTILITY_LLM_CLIENT
LIVE_RUN_POLL_INTERVAL_S = float(
    os.environ.get("MCP_LIVE_RUN_POLL_INTERVAL_S", "0.2")
)
CANCEL_WAIT_TIMEOUT_S = float(os.environ.get("MCP_CANCEL_WAIT_TIMEOUT_S", "5.0"))
RUN_STORE_DB_INITIALIZED = False


def _set_utility_llm_client(client: Any) -> None:
    global UTILITY_LLM_CLIENT
    UTILITY_LLM_CLIENT = client
    runtime_config.UTILITY_LLM_CLIENT = client


_RUN_STORE_BACKEND = RuntimeRunStore(
    run_store_db_path=RUN_STORE_DB_PATH,
    queue_max_attempts=QUEUE_MAX_ATTEMPTS,
    queue_lease_timeout_s=QUEUE_LEASE_TIMEOUT_S,
    utc_now=_utc_now,
)
RUN_STORE: dict[str, dict[str, Any]] = _RUN_STORE_BACKEND.run_store


def _sync_run_store_flags() -> None:
    global RUN_STORE_DB_INITIALIZED
    RUN_STORE_DB_INITIALIZED = _RUN_STORE_BACKEND.db_initialized


def _run_store_connect() -> sqlite3.Connection:
    return _RUN_STORE_BACKEND.run_store_connect()


def _ensure_run_store_initialized() -> None:
    _RUN_STORE_BACKEND.ensure_initialized()
    _sync_run_store_flags()


def _load_run_row(run_id: str) -> sqlite3.Row | None:
    row = _RUN_STORE_BACKEND.load_run_row(run_id)
    _sync_run_store_flags()
    return row


def _inflate_run_record(row: sqlite3.Row) -> dict[str, Any]:
    return _RUN_STORE_BACKEND.inflate_run_record(row)


def _load_run_record(run_id: str) -> dict[str, Any] | None:
    record = _RUN_STORE_BACKEND.load_run_record(run_id)
    _sync_run_store_flags()
    return record


def _upsert_run_row(**kwargs: Any) -> None:
    _RUN_STORE_BACKEND.upsert_run_row(**kwargs)
    _sync_run_store_flags()


def _load_run_job_context(run_id: str) -> dict[str, Any] | None:
    record = _RUN_STORE_BACKEND.load_run_job_context(run_id)
    _sync_run_store_flags()
    return record


def _update_run_metadata(**kwargs: Any) -> None:
    _RUN_STORE_BACKEND.update_run_metadata(**kwargs)
    _sync_run_store_flags()


def _is_cancel_requested(run_id: str) -> bool:
    requested = _RUN_STORE_BACKEND.is_cancel_requested(run_id)
    _sync_run_store_flags()
    return requested


def _lease_owner_id() -> str:
    return _RUN_STORE_BACKEND.lease_owner_id()


def _lease_expires_at() -> str:
    return _RUN_STORE_BACKEND.lease_expires_at()


def _is_lease_expired(value: str | None) -> bool:
    return _RUN_STORE_BACKEND.is_lease_expired(value)


_QUEUE_MANAGER = QueueRuntimeManager(
    QueueRuntimeDeps(
        repo_root=REPO_ROOT,
        entrypoint_path=Path(__file__).resolve(),
        queue_worker_concurrency=QUEUE_WORKER_CONCURRENCY,
        queue_max_attempts=QUEUE_MAX_ATTEMPTS,
        worker_heartbeat_timeout_s=WORKER_HEARTBEAT_TIMEOUT_S,
        utc_now=_utc_now,
        lease_owner_id=lambda: _lease_owner_id(),
        lease_expires_at=lambda: _lease_expires_at(),
        run_store_connect=lambda: _run_store_connect(),
        ensure_run_store_initialized=lambda: _ensure_run_store_initialized(),
        load_run_row=lambda run_id: _load_run_row(run_id),
        inflate_run_record=lambda row: _inflate_run_record(row),
        load_run_record=lambda run_id: _load_run_record(run_id),
        upsert_run_row=lambda **kwargs: _upsert_run_row(**kwargs),
        is_lease_expired=lambda value: _is_lease_expired(value),
        load_terminal_snapshot=_load_terminal_snapshot,
        call_ensure_queue_worker_processes=lambda: _ensure_queue_worker_processes(),
        call_spawn_queue_worker_process=lambda: _spawn_queue_worker_process(),
    )
)
QUEUE_WORKER_PROCESSES: dict[int, subprocess.Popen] = _QUEUE_MANAGER.worker_processes
IS_QUEUE_WORKER_PROCESS = _QUEUE_MANAGER.is_queue_worker_process


def _is_process_alive(pid: int | None) -> bool:
    return _QUEUE_MANAGER.is_process_alive(pid)


def _terminate_pid(pid: int | None) -> None:
    _QUEUE_MANAGER.terminate_pid(pid)


def _queue_worker_command() -> list[str]:
    return _QUEUE_MANAGER.queue_worker_command()


def _spawn_queue_worker_process() -> subprocess.Popen:
    return _QUEUE_MANAGER.spawn_queue_worker_process()


def _reap_queue_worker_processes() -> None:
    _QUEUE_MANAGER.reap_queue_worker_processes()


def _ensure_queue_worker_processes() -> list[int]:
    return _QUEUE_MANAGER.ensure_queue_worker_processes()


def _maybe_recover_terminal_snapshot(
    *,
    run_id: str,
    row: sqlite3.Row | None = None,
    record: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return _QUEUE_MANAGER.maybe_recover_terminal_snapshot(
        run_id=run_id,
        row=row,
        record=record,
    )


def _recover_stale_queued_runs() -> None:
    _QUEUE_MANAGER.recover_stale_queued_runs()


def _claim_next_queued_run() -> str | None:
    return _QUEUE_MANAGER.claim_next_queued_run()


def _recover_stale_running_record(run_id: str) -> dict[str, Any] | None:
    return _QUEUE_MANAGER.recover_stale_running_record(run_id)


_EXECUTION_MANAGER = RuntimeExecutionManager(
    RuntimeExecutionDeps(
        runs_root=RUNS_ROOT,
        default_run_timeout_s=DEFAULT_RUN_TIMEOUT_S,
        default_script_timeout_s=DEFAULT_SCRIPT_TIMEOUT_S,
        max_bootstrap_attempts=MAX_BOOTSTRAP_ATTEMPTS,
        max_repair_attempts=MAX_REPAIR_ATTEMPTS,
        queue_max_attempts=QUEUE_MAX_ATTEMPTS,
        wait_for_terminal_grace_s=WAIT_FOR_TERMINAL_GRACE_S,
        worker_terminal_poll_interval_s=WORKER_TERMINAL_POLL_INTERVAL_S,
        queue_worker_poll_interval_s=QUEUE_WORKER_POLL_INTERVAL_S,
        queue_worker_startup_wait_s=QUEUE_WORKER_STARTUP_WAIT_S,
        live_run_poll_interval_s=LIVE_RUN_POLL_INTERVAL_S,
        utc_now=_utc_now,
        build_run_env=_build_run_env,
        skill_env_runtime_payload=_skill_env_runtime_payload,
        run_preflight=_run_preflight,
        materialize_shell_command=_materialize_shell_command,
        build_run_record_payload=_build_run_record_payload,
        attempt_repair_plan=_attempt_repair_plan,
        build_loaded_skill=_build_loaded_skill,
        load_skill_payload=_load_skill_payload,
        skill_run_record_cls=SkillRunRecord,
        serialized_utility_llm_stub_cls=_SerializedUtilityLLMStub,
        get_llm_client=lambda: UTILITY_LLM_CLIENT,
        set_llm_client=_set_utility_llm_client,
        prepare_transport_payload=_prepare_transport_payload,
        materialize_skill_workspace_view=_materialize_skill_workspace_view,
        write_mirrored_skill_manifest=_write_mirrored_skill_manifest,
        write_skill_request=_write_skill_request,
        collect_workspace_artifacts=_collect_workspace_artifacts,
        truncate_log=_truncate_log,
        write_terminal_snapshot=_write_terminal_snapshot,
        load_run_record=lambda run_id: _load_run_record(run_id),
        upsert_run_row=lambda **kwargs: _upsert_run_row(**kwargs),
        update_run_metadata=lambda **kwargs: _update_run_metadata(**kwargs),
        load_run_job_context=lambda run_id: _load_run_job_context(run_id),
        is_cancel_requested=lambda run_id: _is_cancel_requested(run_id),
        run_store=RUN_STORE,
        call_ensure_queue_worker_processes=lambda: _ensure_queue_worker_processes(),
        call_recover_stale_running_record=lambda run_id: _recover_stale_running_record(
            run_id
        ),
        call_claim_next_queued_run=lambda: _claim_next_queued_run(),
        call_run_durable_worker=lambda run_id: _run_durable_worker(run_id),
    )
)


def _store_run_record(record: dict[str, Any]) -> None:
    _EXECUTION_MANAGER.store_run_record(record)


def _update_live_run_record(**kwargs: Any) -> None:
    _EXECUTION_MANAGER.update_live_run_record(**kwargs)


def _durable_runtime_metadata(**extra: Any) -> dict[str, Any]:
    return _EXECUTION_MANAGER.durable_runtime_metadata(**extra)


def _runtime_metadata_with_skill_env(
    *,
    runtime: dict[str, Any] | None = None,
    skill_env_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _EXECUTION_MANAGER.runtime_metadata_with_skill_env(
        runtime=runtime,
        skill_env_spec=skill_env_spec,
    )


def _mark_run_cancel_requested(run_id: str) -> None:
    _EXECUTION_MANAGER.mark_run_cancel_requested(run_id)


def _resolve_wait_for_completion(
    *,
    constraints: dict[str, Any],
    wait_for_completion: bool | None,
) -> bool:
    return _EXECUTION_MANAGER.resolve_wait_for_completion(
        constraints=constraints,
        wait_for_completion=wait_for_completion,
    )


def _store_attempt_snapshot(
    *,
    base_run_id: str,
    suffix: str,
    record: dict[str, Any],
) -> str:
    return _EXECUTION_MANAGER.store_attempt_snapshot(
        base_run_id=base_run_id,
        suffix=suffix,
        record=record,
    )


def _remove_workspace_files(
    *,
    workspace_dir: Path,
    relative_paths: list[str],
) -> None:
    _EXECUTION_MANAGER.remove_workspace_files(
        workspace_dir=workspace_dir,
        relative_paths=relative_paths,
    )


def _failure_reason_from_execution(execution: dict[str, Any]) -> str:
    return _EXECUTION_MANAGER.failure_reason_from_execution(execution)


def _failed_execution_payload(
    *,
    workspace_dir: Path,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    timed_out: bool = False,
    cancel_requested: bool = False,
) -> dict[str, Any]:
    return _EXECUTION_MANAGER.failed_execution_payload(
        workspace_dir=workspace_dir,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        cancel_requested=cancel_requested,
    )


def _build_repair_history_entry(
    *,
    attempt_index: int,
    stage: str,
    snapshot_run_id: str,
    command_plan: SkillCommandPlan,
    command: str | None,
    preflight: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    return _EXECUTION_MANAGER.build_repair_history_entry(
        attempt_index=attempt_index,
        stage=stage,
        snapshot_run_id=snapshot_run_id,
        command_plan=command_plan,
        command=command,
        preflight=preflight,
        execution=execution,
    )


def _build_bootstrap_history_entry(
    *,
    attempt_index: int,
    commands: list[str],
    result: dict[str, Any],
) -> dict[str, Any]:
    return _EXECUTION_MANAGER.build_bootstrap_history_entry(
        attempt_index=attempt_index,
        commands=commands,
        result=result,
    )


def _bootstrap_commands_require_network(commands: list[str]) -> bool:
    return _EXECUTION_MANAGER.bootstrap_commands_require_network(commands)


def _bootstrap_failure_payload(
    *,
    commands: list[str],
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    failure_reason: str = "bootstrap_failed",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    return _EXECUTION_MANAGER.bootstrap_failure_payload(
        commands=commands,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        failure_reason=failure_reason,
        started_at=started_at,
        finished_at=finished_at,
    )


async def _execute_bootstrap_commands(
    *,
    commands: list[str],
    workspace_dir: Path,
    env: dict[str, str],
    timeout_s: int,
) -> dict[str, Any]:
    return await _EXECUTION_MANAGER.execute_bootstrap_commands(
        commands=commands,
        workspace_dir=workspace_dir,
        env=env,
        timeout_s=timeout_s,
    )


async def _execute_shell_command(
    *,
    run_id: str,
    shell_command: str,
    workspace_dir: Path,
    env: dict[str, str],
    timeout_s: int,
) -> dict[str, Any]:
    return await _EXECUTION_MANAGER.execute_shell_command(
        run_id=run_id,
        shell_command=shell_command,
        workspace_dir=workspace_dir,
        env=env,
        timeout_s=timeout_s,
    )


async def _complete_shell_run(**kwargs: Any) -> dict[str, Any]:
    return await _EXECUTION_MANAGER.complete_shell_run(**kwargs)


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
    return _EXECUTION_MANAGER.build_worker_job_context(
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


async def _wait_for_terminal_run_record(
    *,
    run_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    return await _EXECUTION_MANAGER.wait_for_terminal_run_record(
        run_id=run_id,
        timeout_s=timeout_s,
    )


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
    return await _EXECUTION_MANAGER.run_shell_task(
        skill_payload=skill_payload,
        loaded_skill=loaded_skill,
        request=request,
        command_plan=command_plan,
        timeout_s=timeout_s,
        cleanup_workspace=cleanup_workspace,
        wait_for_completion=wait_for_completion,
    )


async def _run_durable_worker(run_id: str) -> int:
    return await _EXECUTION_MANAGER.run_durable_worker(run_id)


async def _run_queue_worker_loop() -> int:
    return await _EXECUTION_MANAGER.run_queue_worker_loop()


_SERVICE = RuntimeService(
    RuntimeServiceDeps(
        build_skill_catalog_entry=_build_skill_catalog_entry,
        build_skill_runtime_context=_build_skill_runtime_context,
        build_command=_build_command,
        build_loaded_skill=_build_loaded_skill,
        build_loaded_skill_from_dir=_build_loaded_skill_from_dir,
        build_run_record_payload=_build_run_record_payload,
        build_script_env=_build_script_env,
        classify_skill_file=_classify_skill_file,
        default_run_timeout_s=DEFAULT_RUN_TIMEOUT_S,
        default_runtime_target=DEFAULT_RUNTIME_TARGET,
        default_script_timeout_s=DEFAULT_SCRIPT_TIMEOUT_S,
        get_llm_client=lambda: UTILITY_LLM_CLIENT,
        load_run_row=_load_run_row,
        load_skill_payload=_load_skill_payload,
        materialize_shell_command=_materialize_shell_command,
        mark_run_cancel_requested=_mark_run_cancel_requested,
        max_reference_bytes=MAX_REFERENCE_BYTES,
        now=_utc_now,
        prefetch_skill_wheels_for_skill=_prefetch_skill_wheels_for_skill,
        prepare_transport_payload=_prepare_transport_payload,
        read_text=_read_text,
        recover_stale_running_record=_recover_stale_running_record,
        request_with_runtime_target=_request_with_runtime_target,
        resolve_command_plan=_resolve_command_plan,
        resolve_script_path=_resolve_script_path,
        resolve_skill_dir=_resolve_skill_dir,
        resolve_skill_file_path=_resolve_skill_file_path,
        resolve_wait_for_completion=_resolve_wait_for_completion,
        run_shell_task=_run_shell_task,
        runs_root=RUNS_ROOT,
        score_skill_match=_score_skill_match,
        skill_dirs=_skill_dirs,
        skill_run_record_cls=SkillRunRecord,
        store_run_record=_store_run_record,
        terminate_pid=_terminate_pid,
        wait_for_terminal_run_record=_wait_for_terminal_run_record,
        write_json_atomic=_write_json_atomic,
        cancel_wait_timeout_s=CANCEL_WAIT_TIMEOUT_S,
        call_tool_result_cls=CallToolResult,
        text_content_cls=TextContent,
    )
)

_TRANSPORT = build_transport_bindings(
    mcp=mcp,
    service=_SERVICE,
    default_run_timeout_s=DEFAULT_RUN_TIMEOUT_S,
    default_script_timeout_s=DEFAULT_SCRIPT_TIMEOUT_S,
)

list_skills = _TRANSPORT.list_skills
read_skill = _TRANSPORT.read_skill
read_skill_file = _TRANSPORT.read_skill_file
read_skill_docs = _TRANSPORT.read_skill_docs
run_skill_task = _TRANSPORT.run_skill_task
get_run_status = _TRANSPORT.get_run_status
cancel_skill_run = _TRANSPORT.cancel_skill_run
get_run_logs = _TRANSPORT.get_run_logs
get_run_artifacts = _TRANSPORT.get_run_artifacts
execute_skill_script = _TRANSPORT.execute_skill_script
skill_catalog_resource = _TRANSPORT.skill_catalog_resource
skill_resource = _TRANSPORT.skill_resource
skill_docs_resource = _TRANSPORT.skill_docs_resource
skill_file_resource = _TRANSPORT.skill_file_resource
skill_reference_resource = _TRANSPORT.skill_reference_resource
skill_run_logs_resource = _TRANSPORT.skill_run_logs_resource
skill_run_artifacts_resource = _TRANSPORT.skill_run_artifacts_resource


def _prefetch_skill_wheels_cli(*, skill_name: str | None = None) -> dict[str, Any]:
    payload = _SERVICE.prefetch_skill_wheels_cli(skill_name=skill_name)
    payload["wheelhouse_root"] = str(WHEELHOUSE_ROOT)
    payload["pip_cache_dir"] = str(PIP_CACHE_ROOT)
    return payload


def main() -> None:
    run_main(
        deps=RuntimeCliDeps(
            workspace_root=WORKSPACE_ROOT,
            runs_root=RUNS_ROOT,
            state_root=STATE_ROOT,
            envs_root=ENVS_ROOT,
            wheelhouse_root=WHEELHOUSE_ROOT,
            pip_cache_root=PIP_CACHE_ROOT,
            locks_root=LOCKS_ROOT,
            ensure_run_store_initialized=_ensure_run_store_initialized,
            prefetch_skill_wheels_cli=_prefetch_skill_wheels_cli,
            run_queue_worker_loop=_run_queue_worker_loop,
            run_durable_worker=_run_durable_worker,
            ensure_queue_worker_processes=_ensure_queue_worker_processes,
            mcp=mcp,
        )
    )


if __name__ == "__main__":
    main()
