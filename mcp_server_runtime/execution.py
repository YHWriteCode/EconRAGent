from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from kg_agent.skills.command_planner import is_dry_run
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillRunRecord,
    SkillRuntimeTarget,
)

from .errors import SkillServerError
from .utils import build_shell_exec_argv, utc_duration_seconds


BOOTSTRAP_NETWORK_INSTALL_PATTERN = re.compile(
    r"("
    r"\bpip(?:3)?\s+install\b|"
    r"\bpython\s+-m\s+pip\s+install\b|"
    r"\buv\s+pip\s+install\b|"
    r"\bnpm\s+install\b|"
    r"\byarn\s+add\b|"
    r"\bpnpm\s+add\b|"
    r"\bapt(?:-get)?\s+install\b|"
    r"\byum\s+install\b|"
    r"\bdnf\s+install\b|"
    r"\bapk\s+add\b|"
    r"\bbrew\s+install\b|"
    r"\bwinget\s+install\b|"
    r"\bchoco\s+install\b|"
    r"\bcurl\b|"
    r"\bwget\b|"
    r"Invoke-WebRequest|"
    r"Start-BitsTransfer"
    r")",
    re.IGNORECASE,
)
NODE_BOOTSTRAP_COMMAND_PATTERN = re.compile(
    r"\b(?:node|npm|npx|yarn|pnpm)\b",
    re.IGNORECASE,
)
PIP_INSTALL_COMMAND_PATTERN = re.compile(
    r"\b(?:python\s+-m\s+pip|pip(?:3)?)\s+install\b",
    re.IGNORECASE,
)
UV_PIP_INSTALL_COMMAND_PATTERN = re.compile(
    r"\buv\s+pip\s+install\b",
    re.IGNORECASE,
)
DEFAULT_BOOTSTRAP_NODE_VERSION = os.environ.get(
    "MCP_BOOTSTRAP_NODE_VERSION",
    "20.19.5",
).strip() or "20.19.5"
DEFAULT_BOOTSTRAP_NODE_BASE_URL = os.environ.get(
    "MCP_BOOTSTRAP_NODE_BASE_URL",
    "https://nodejs.org/dist",
).strip() or "https://nodejs.org/dist"
DEFAULT_BOOTSTRAP_NODE_DOWNLOAD_TIMEOUT_S = max(
    10.0,
    float(os.environ.get("MCP_BOOTSTRAP_NODE_DOWNLOAD_TIMEOUT_S", "120.0")),
)


@dataclass
class RuntimeExecutionDeps:
    runs_root: Path
    default_run_timeout_s: int
    default_script_timeout_s: int
    max_bootstrap_attempts: int
    max_repair_attempts: int
    queue_max_attempts: int
    wait_for_terminal_grace_s: float
    worker_terminal_poll_interval_s: float
    queue_worker_poll_interval_s: float
    queue_worker_startup_wait_s: float
    live_run_poll_interval_s: float
    utc_now: Callable[[], str]
    build_run_env: Callable[..., tuple[dict[str, str], dict[str, Any] | None]]
    skill_env_runtime_payload: Callable[[dict[str, Any] | None], dict[str, Any]]
    run_preflight: Callable[..., tuple[dict[str, Any], list[str]]]
    materialize_shell_command: Callable[..., str | None]
    build_run_record_payload: Callable[..., dict[str, Any]]
    attempt_repair_plan: Callable[..., Awaitable[SkillCommandPlan | None]]
    build_loaded_skill: Callable[[str], LoadedSkill]
    load_skill_payload: Callable[[str], dict[str, Any]]
    skill_run_record_cls: type[SkillRunRecord]
    serialized_utility_llm_stub_cls: type[Any]
    get_llm_client: Callable[[], Any]
    set_llm_client: Callable[[Any], None]
    refresh_llm_client: Callable[[], Any] | None
    prepare_transport_payload: Callable[[dict[str, Any]], dict[str, Any]]
    materialize_skill_workspace_view: Callable[..., list[str]]
    write_mirrored_skill_manifest: Callable[..., None]
    write_skill_request: Callable[..., Path]
    write_skill_context: Callable[..., Path]
    collect_workspace_artifacts: Callable[[Path], list[dict[str, Any]]]
    truncate_log: Callable[[str], tuple[str, bool]]
    write_terminal_snapshot: Callable[..., None]
    load_run_record: Callable[[str], dict[str, Any] | None]
    upsert_run_row: Callable[..., None]
    update_run_metadata: Callable[..., None]
    load_run_job_context: Callable[[str], dict[str, Any] | None]
    is_cancel_requested: Callable[[str], bool]
    run_store: dict[str, dict[str, Any]]
    call_ensure_queue_worker_processes: Callable[[], list[int]]
    call_recover_stale_running_record: Callable[[str], dict[str, Any] | None]
    call_claim_next_queued_run: Callable[[], str | None]
    call_run_durable_worker: Callable[[str], Awaitable[int]]


class RuntimeExecutionManager:
    def __init__(self, deps: RuntimeExecutionDeps) -> None:
        self.deps = deps

    def get_repair_llm_client(self) -> Any | None:
        llm_client = self.deps.get_llm_client()
        if llm_client is not None and llm_client.is_available():
            return llm_client
        refresh = self.deps.refresh_llm_client
        if not callable(refresh):
            return llm_client
        try:
            refreshed_client = refresh()
        except Exception:
            return llm_client
        if refreshed_client is None:
            return llm_client
        try:
            self.deps.set_llm_client(refreshed_client)
        except Exception:
            return llm_client
        return refreshed_client

    @staticmethod
    def _normalize_bootstrap_command_for_reuse(command: str) -> str:
        normalized = str(command or "").strip()
        if not normalized:
            return normalized
        lower = normalized.lower()
        if "--upgrade" in lower:
            return normalized
        if PIP_INSTALL_COMMAND_PATTERN.search(normalized):
            return PIP_INSTALL_COMMAND_PATTERN.sub(
                lambda match: match.group(0) + " --upgrade --upgrade-strategy only-if-needed",
                normalized,
                count=1,
            )
        if UV_PIP_INSTALL_COMMAND_PATTERN.search(normalized):
            return UV_PIP_INSTALL_COMMAND_PATTERN.sub(
                lambda match: match.group(0) + " --upgrade",
                normalized,
                count=1,
            )
        return normalized

    @staticmethod
    def _bootstrap_commands_need_node_runtime(commands: list[str]) -> bool:
        return any(
            NODE_BOOTSTRAP_COMMAND_PATTERN.search(str(command or ""))
            for command in commands
        )

    @staticmethod
    def _node_runtime_platform_tag() -> tuple[str, str] | None:
        system_name = platform.system().lower()
        machine = platform.machine().lower()
        if system_name == "linux":
            if machine in {"x86_64", "amd64"}:
                return "linux", "x64"
            if machine in {"aarch64", "arm64"}:
                return "linux", "arm64"
        if system_name == "darwin":
            if machine in {"x86_64", "amd64"}:
                return "darwin", "x64"
            if machine in {"arm64", "aarch64"}:
                return "darwin", "arm64"
        if system_name == "windows":
            if machine in {"x86_64", "amd64"}:
                return "win", "x64"
            if machine in {"arm64", "aarch64"}:
                return "win", "arm64"
        return None

    @staticmethod
    def _prepend_path_entry(env: dict[str, str], entry: Path) -> None:
        existing = str(env.get("PATH", "") or "")
        entry_text = str(entry)
        parts = [item for item in existing.split(os.pathsep) if item]
        if entry_text not in parts:
            env["PATH"] = os.pathsep.join([entry_text, *parts]) if parts else entry_text

    @staticmethod
    def _safe_extract_tarfile(archive: tarfile.TarFile, destination: Path) -> None:
        destination_resolved = destination.resolve()
        for member in archive.getmembers():
            target_path = (destination / member.name).resolve()
            if target_path != destination_resolved and destination_resolved not in target_path.parents:
                raise RuntimeError(f"Unsafe archive member path: {member.name}")
        archive.extractall(destination)

    def _resolve_bootstrap_node_paths(
        self,
        *,
        env: dict[str, str],
        version: str,
    ) -> tuple[Path, Path, Path] | None:
        root_text = str(env.get("SKILL_BOOTSTRAP_ROOT", "")).strip()
        if not root_text:
            return None
        platform_tags = self._node_runtime_platform_tag()
        if platform_tags is None:
            return None
        platform_tag, arch_tag = platform_tags
        base_root = Path(root_text).resolve() / ".node-runtime"
        install_root = base_root / f"node-v{version}-{platform_tag}-{arch_tag}"
        bin_dir = install_root if platform_tag == "win" else install_root / "bin"
        npm_path = (
            bin_dir / "npm.cmd"
            if platform_tag == "win"
            else bin_dir / "npm"
        )
        return install_root, bin_dir, npm_path

    async def ensure_bootstrap_node_runtime(
        self,
        *,
        env: dict[str, str],
        timeout_s: int,
    ) -> dict[str, Any]:
        if shutil.which("node", path=env.get("PATH")) and shutil.which("npm", path=env.get("PATH")):
            finished_at = self.deps.utc_now()
            return {
                "success": True,
                "stdout": "",
                "stderr": "",
                "failure_reason": None,
                "started_at": finished_at,
                "finished_at": finished_at,
                "duration_s": 0.0,
            }
        if str(env.get("SKILL_NETWORK_ALLOWED", "false")).strip().lower() != "true":
            started_at = self.deps.utc_now()
            return {
                "success": False,
                "stdout": "",
                "stderr": "Node/npm is required for this bootstrap step but network access is disabled.\n",
                "failure_reason": "bootstrap_network_not_allowed",
                "started_at": started_at,
                "finished_at": self.deps.utc_now(),
                "duration_s": utc_duration_seconds(started_at, self.deps.utc_now()),
            }
        return await asyncio.to_thread(
            self._ensure_bootstrap_node_runtime_sync,
            env,
            timeout_s,
        )

    def _ensure_bootstrap_node_runtime_sync(
        self,
        env: dict[str, str],
        timeout_s: int,
    ) -> dict[str, Any]:
        started_at = self.deps.utc_now()
        version = DEFAULT_BOOTSTRAP_NODE_VERSION
        resolved_paths = self._resolve_bootstrap_node_paths(env=env, version=version)
        if resolved_paths is None:
            finished_at = self.deps.utc_now()
            return {
                "success": False,
                "stdout": "",
                "stderr": "Automatic Node.js bootstrap is not supported for this platform.\n",
                "failure_reason": "bootstrap_node_runtime_unsupported",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": utc_duration_seconds(started_at, finished_at),
            }
        install_root, bin_dir, npm_path = resolved_paths
        node_path = bin_dir / ("node.exe" if platform.system().lower() == "windows" else "node")
        install_root.parent.mkdir(parents=True, exist_ok=True)

        if node_path.exists() and npm_path.exists():
            self._prepend_path_entry(env, bin_dir)
            finished_at = self.deps.utc_now()
            return {
                "success": True,
                "stdout": f"[bootstrap runtime] Reusing Node.js runtime at {install_root}\n",
                "stderr": "",
                "failure_reason": None,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": utc_duration_seconds(started_at, finished_at),
            }

        platform_tags = self._node_runtime_platform_tag()
        assert platform_tags is not None
        platform_tag, arch_tag = platform_tags
        if platform_tag == "win":
            archive_name = f"node-v{version}-win-{arch_tag}.zip"
        else:
            archive_name = f"node-v{version}-{platform_tag}-{arch_tag}.tar.xz"
        download_url = f"{DEFAULT_BOOTSTRAP_NODE_BASE_URL}/v{version}/{archive_name}"
        temp_dir = Path(tempfile.mkdtemp(prefix="node-bootstrap-", dir=str(install_root.parent)))
        archive_path = temp_dir / archive_name
        stdout_chunks = [f"[bootstrap runtime] Downloading Node.js from {download_url}\n"]
        stderr_text = ""
        try:
            request = Request(
                download_url,
                headers={"User-Agent": "lightrag-skill-runtime/1.0"},
            )
            with urlopen(request, timeout=max(10.0, min(float(timeout_s), DEFAULT_BOOTSTRAP_NODE_DOWNLOAD_TIMEOUT_S))) as response:
                archive_path.write_bytes(response.read())
            extract_root = temp_dir / "extract"
            extract_root.mkdir(parents=True, exist_ok=True)
            if archive_name.endswith(".zip"):
                import zipfile

                with zipfile.ZipFile(archive_path) as archive:
                    archive.extractall(extract_root)
            else:
                with tarfile.open(archive_path, "r:*") as archive:
                    self._safe_extract_tarfile(archive, extract_root)
            extracted_dirs = [item for item in extract_root.iterdir() if item.is_dir()]
            if not extracted_dirs:
                raise RuntimeError("Downloaded Node.js archive did not contain an installable directory")
            extracted_root = extracted_dirs[0]
            if install_root.exists():
                shutil.rmtree(install_root, ignore_errors=True)
            shutil.move(str(extracted_root), str(install_root))
            if not node_path.exists() or not npm_path.exists():
                raise RuntimeError("Downloaded Node.js runtime is missing node or npm binaries")
            self._prepend_path_entry(env, bin_dir)
            stdout_chunks.append(f"[bootstrap runtime] Installed Node.js runtime at {install_root}\n")
            finished_at = self.deps.utc_now()
            return {
                "success": True,
                "stdout": "".join(stdout_chunks),
                "stderr": "",
                "failure_reason": None,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": utc_duration_seconds(started_at, finished_at),
            }
        except Exception as exc:
            stderr_text = f"Failed to provision Node.js runtime automatically: {exc}\n"
            finished_at = self.deps.utc_now()
            return {
                "success": False,
                "stdout": "".join(stdout_chunks),
                "stderr": stderr_text,
                "failure_reason": "bootstrap_node_runtime_failed",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": utc_duration_seconds(started_at, finished_at),
            }
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def store_run_record(self, record: dict[str, Any]) -> None:
        run_id = str(record.get("run_id", "")).strip()
        if run_id:
            self.deps.upsert_run_row(run_id=run_id, record=record)

    def update_live_run_record(
        self,
        *,
        run_id: str,
        workspace_dir: Path | None,
        stdout_text: str,
        stderr_text: str,
        summary: str | None = None,
        cancel_requested: bool | None = None,
        refresh_artifacts: bool = True,
    ) -> None:
        record = self.deps.load_run_record(run_id)
        if not isinstance(record, dict):
            return

        stdout_preview, stdout_truncated = self.deps.truncate_log(stdout_text)
        stderr_preview, stderr_truncated = self.deps.truncate_log(stderr_text)
        record["logs"] = {"stdout": stdout_text, "stderr": stderr_text}
        record["logs_preview"] = {
            "stdout": stdout_preview,
            "stderr": stderr_preview,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        if refresh_artifacts and isinstance(workspace_dir, Path) and workspace_dir.exists():
            record["artifacts"] = self.deps.collect_workspace_artifacts(workspace_dir)
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
        runtime["log_streaming"] = False
        runtime["log_transport"] = "poll"
        if cancel_requested is not None:
            runtime["cancel_requested"] = cancel_requested
        record["runtime"] = runtime
        self.deps.upsert_run_row(
            run_id=run_id,
            record=record,
            queue_state=str(runtime.get("queue_state", "")).strip() or None,
            cancel_requested=record.get("cancel_requested"),
            heartbeat=True,
        )

    @staticmethod
    def durable_runtime_metadata(**extra: Any) -> dict[str, Any]:
        runtime = {
            "executor": "shell",
            "delivery": "durable_worker",
            "store_backend": "sqlite",
            "cancel_supported": True,
            "log_streaming": False,
            "log_transport": "poll",
        }
        runtime.update(extra)
        return runtime

    def runtime_metadata_with_skill_env(
        self,
        *,
        runtime: dict[str, Any] | None = None,
        skill_env_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(runtime or {})
        payload["skill_environment"] = self.deps.skill_env_runtime_payload(skill_env_spec)
        return payload

    def mark_run_cancel_requested(self, run_id: str) -> None:
        record = self.deps.load_run_record(run_id)
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
        self.update_live_run_record(
            run_id=run_id,
            workspace_dir=workspace_dir,
            stdout_text=stdout,
            stderr_text=stderr,
            summary=f"Cancellation requested for run '{run_id}'.",
            cancel_requested=True,
            refresh_artifacts=False,
        )
        self.deps.update_run_metadata(
            run_id=run_id,
            queue_state="cancelling",
            cancel_requested=True,
            heartbeat=True,
        )

    @staticmethod
    def resolve_wait_for_completion(
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

    def store_attempt_snapshot(
        self,
        *,
        base_run_id: str,
        suffix: str,
        record: dict[str, Any],
    ) -> str:
        snapshot_run_id = f"{base_run_id}:{suffix}"
        snapshot = dict(record)
        snapshot["run_id"] = snapshot_run_id
        self.deps.run_store[snapshot_run_id] = snapshot
        return snapshot_run_id

    @staticmethod
    def remove_workspace_files(
        *,
        workspace_dir: Path,
        relative_paths: list[str],
    ) -> None:
        for raw_path in relative_paths:
            relative_path = str(raw_path or "").replace("\\", "/").strip()
            if not relative_path:
                continue
            target = (workspace_dir / relative_path).resolve()
            if target == workspace_dir or workspace_dir not in target.parents:
                continue
            if target.is_file():
                target.unlink(missing_ok=True)

    @staticmethod
    def failure_reason_from_execution(execution: dict[str, Any]) -> str:
        if bool(execution.get("cancelled") or execution.get("cancel_requested")):
            return "cancelled"
        if bool(execution.get("timed_out")):
            return "timed_out"
        return "process_failed"

    def failed_execution_payload(
        self,
        *,
        workspace_dir: Path,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        timed_out: bool = False,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        stdout_preview, stdout_truncated = self.deps.truncate_log(stdout)
        stderr_preview, stderr_truncated = self.deps.truncate_log(stderr)
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cancel_requested": cancel_requested,
            "cancelled": cancel_requested and not timed_out,
            "success": False,
            "stdout": stdout,
            "stderr": stderr,
            "logs_preview": {
                "stdout": stdout_preview,
                "stderr": stderr_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
            "artifacts": self.deps.collect_workspace_artifacts(workspace_dir),
        }

    def build_repair_history_entry(
        self,
        *,
        attempt_index: int,
        stage: str,
        snapshot_run_id: str,
        command_plan: SkillCommandPlan,
        command: str | None,
        preflight: dict[str, Any],
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "attempt_index": attempt_index,
            "snapshot_run_id": snapshot_run_id,
            "stage": stage,
            "plan_mode": command_plan.mode,
            "entrypoint": command_plan.entrypoint,
            "generated_files": [item.path for item in command_plan.generated_files],
            "command": command,
            "preflight_status": preflight.get("status"),
            "preflight_failure_reason": preflight.get("failure_reason"),
            "failure_reason": (
                preflight.get("failure_reason")
                if preflight.get("failure_reason")
                else self.failure_reason_from_execution(execution)
            ),
            "exit_code": execution.get("exit_code"),
            "started_at": execution.get("started_at"),
            "finished_at": execution.get("finished_at"),
            "duration_s": execution.get("duration_s"),
            "stdout_tail": str(execution.get("stdout", ""))[-2000:],
            "stderr_tail": str(execution.get("stderr", ""))[-2000:],
        }

    @staticmethod
    def build_bootstrap_history_entry(
        *,
        attempt_index: int,
        commands: list[str],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "attempt_index": attempt_index,
            "commands": list(commands),
            "success": bool(result.get("success")),
            "failure_reason": result.get("failure_reason"),
            "exit_code": result.get("exit_code"),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "duration_s": result.get("duration_s"),
            "stdout_tail": str(result.get("stdout", ""))[-2000:],
            "stderr_tail": str(result.get("stderr", ""))[-2000:],
        }

    @staticmethod
    def bootstrap_commands_require_network(commands: list[str]) -> bool:
        return any(
            BOOTSTRAP_NETWORK_INSTALL_PATTERN.search(str(command or ""))
            for command in commands
        )

    def bootstrap_failure_payload(
        self,
        *,
        commands: list[str],
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        failure_reason: str = "bootstrap_failed",
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        stdout_preview, stdout_truncated = self.deps.truncate_log(stdout)
        stderr_preview, stderr_truncated = self.deps.truncate_log(stderr)
        return {
            "commands": list(commands),
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "success": False,
            "failure_reason": failure_reason,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": utc_duration_seconds(started_at, finished_at),
            "logs_preview": {
                "stdout": stdout_preview,
                "stderr": stderr_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        }

    async def execute_bootstrap_commands(
        self,
        *,
        commands: list[str],
        workspace_dir: Path,
        env: dict[str, str],
        timeout_s: int,
    ) -> dict[str, Any]:
        run_started_at = self.deps.utc_now()
        if not commands:
            run_finished_at = self.deps.utc_now()
            return {
                "commands": [],
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "success": True,
                "failure_reason": None,
                "started_at": run_started_at,
                "finished_at": run_finished_at,
                "duration_s": utc_duration_seconds(run_started_at, run_finished_at),
                "logs_preview": {
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
            }
        if (
            not env.get("SKILL_BOOTSTRAP_ROOT")
            or self.bootstrap_commands_require_network(commands)
            and str(env.get("SKILL_NETWORK_ALLOWED", "false")).strip().lower()
            != "true"
        ):
            return self.bootstrap_failure_payload(
                commands=commands,
                failure_reason="bootstrap_network_not_allowed",
                started_at=run_started_at,
                finished_at=self.deps.utc_now(),
            )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_exit_code = 0
        per_command_timeout = max(
            1,
            min(int(timeout_s), self.deps.default_script_timeout_s),
        )
        if self._bootstrap_commands_need_node_runtime(commands):
            node_bootstrap = await self.ensure_bootstrap_node_runtime(
                env=env,
                timeout_s=per_command_timeout,
            )
            stdout_chunks.append(str(node_bootstrap.get("stdout", "")))
            stderr_chunks.append(str(node_bootstrap.get("stderr", "")))
            if not node_bootstrap.get("success"):
                return self.bootstrap_failure_payload(
                    commands=commands,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    exit_code=127,
                    failure_reason=str(
                        node_bootstrap.get("failure_reason") or "bootstrap_node_runtime_failed"
                    ),
                    started_at=run_started_at,
                    finished_at=self.deps.utc_now(),
                )
        for index, command in enumerate(commands, start=1):
            effective_command = self._normalize_bootstrap_command_for_reuse(command)
            marker = f"[bootstrap {index}/{len(commands)}] {effective_command}\n"
            stdout_chunks.append(marker)
            process = await asyncio.create_subprocess_exec(
                *build_shell_exec_argv(effective_command),
                cwd=str(workspace_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=per_command_timeout,
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                return self.bootstrap_failure_payload(
                    commands=commands,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks)
                    + f"Bootstrap command timed out: {effective_command}\n",
                    exit_code=124,
                    failure_reason="bootstrap_timed_out",
                    started_at=run_started_at,
                    finished_at=self.deps.utc_now(),
                )
            stdout_chunks.append(stdout_bytes.decode("utf-8", errors="replace"))
            stderr_chunks.append(stderr_bytes.decode("utf-8", errors="replace"))
            last_exit_code = process.returncode or 0
            if last_exit_code != 0:
                return self.bootstrap_failure_payload(
                    commands=commands,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    exit_code=last_exit_code,
                    failure_reason="bootstrap_failed",
                    started_at=run_started_at,
                    finished_at=self.deps.utc_now(),
                )

        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        stdout_preview, stdout_truncated = self.deps.truncate_log(stdout_text)
        stderr_preview, stderr_truncated = self.deps.truncate_log(stderr_text)
        run_finished_at = self.deps.utc_now()
        return {
            "commands": list(commands),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": last_exit_code,
            "success": True,
            "failure_reason": None,
            "started_at": run_started_at,
            "finished_at": run_finished_at,
            "duration_s": utc_duration_seconds(run_started_at, run_finished_at),
            "logs_preview": {
                "stdout": stdout_preview,
                "stderr": stderr_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        }

    async def execute_shell_command(
        self,
        *,
        run_id: str,
        shell_command: str,
        workspace_dir: Path,
        env: dict[str, str],
        timeout_s: int,
    ) -> dict[str, Any]:
        execution_started_at = self.deps.utc_now()
        process = await asyncio.create_subprocess_exec(
            *build_shell_exec_argv(shell_command),
            cwd=str(workspace_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        cancel_state = {"requested": self.deps.is_cancel_requested(run_id)}
        self.deps.update_run_metadata(
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
                self.update_live_run_record(
                    run_id=run_id,
                    workspace_dir=workspace_dir,
                    stdout_text="".join(stdout_chunks),
                    stderr_text="".join(stderr_chunks),
                    cancel_requested=self.deps.is_cancel_requested(run_id),
                    refresh_artifacts=False,
                )
                if self.deps.is_cancel_requested(run_id) and process.returncode is None:
                    try:
                        cancel_state["requested"] = True
                        process.kill()
                    except ProcessLookupError:
                        pass

        async def _poll_live_state() -> None:
            try:
                while process.returncode is None:
                    cancel_state["requested"] = self.deps.is_cancel_requested(run_id)
                    self.update_live_run_record(
                        run_id=run_id,
                        workspace_dir=workspace_dir,
                        stdout_text="".join(stdout_chunks),
                        stderr_text="".join(stderr_chunks),
                        cancel_requested=cancel_state["requested"],
                        refresh_artifacts=True,
                    )
                    self.deps.update_run_metadata(
                        run_id=run_id,
                        queue_state=(
                            "cancelling" if cancel_state["requested"] else "executing"
                        ),
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
                    await asyncio.sleep(max(0.05, self.deps.live_run_poll_interval_s))
            except asyncio.CancelledError:
                return

        stdout_task = asyncio.create_task(_consume_stream(process.stdout, stdout_chunks))
        stderr_task = asyncio.create_task(_consume_stream(process.stderr, stderr_chunks))
        poll_task = asyncio.create_task(_poll_live_state())
        timed_out = False
        try:
            if self.deps.is_cancel_requested(run_id) and process.returncode is None:
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
        stdout_preview, stdout_truncated = self.deps.truncate_log(stdout_text)
        stderr_preview, stderr_truncated = self.deps.truncate_log(stderr_text)
        cancel_requested = bool(
            cancel_state["requested"] or self.deps.is_cancel_requested(run_id)
        )
        exit_code = process.returncode
        if cancel_requested and not timed_out and exit_code in {None, 0}:
            exit_code = 130
        elif timed_out and exit_code in {None, 0}:
            exit_code = 124
        self.update_live_run_record(
            run_id=run_id,
            workspace_dir=workspace_dir,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            cancel_requested=cancel_requested,
            refresh_artifacts=True,
        )
        self.deps.update_run_metadata(
            run_id=run_id,
            queue_state=(
                "failed" if (cancel_requested or timed_out or exit_code != 0) else "completed"
            ),
            worker_pid=os.getpid(),
            active_process_pid=None,
            cancel_requested=cancel_requested,
            heartbeat=True,
        )
        execution_finished_at = self.deps.utc_now()
        return {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "cancel_requested": cancel_requested,
            "cancelled": cancel_requested and not timed_out,
            "success": (exit_code == 0) and not timed_out and not cancel_requested,
            "started_at": execution_started_at,
            "finished_at": execution_finished_at,
            "duration_s": utc_duration_seconds(execution_started_at, execution_finished_at),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "logs_preview": {
                "stdout": stdout_preview,
                "stderr": stderr_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
            "artifacts": self.deps.collect_workspace_artifacts(workspace_dir),
        }

    async def complete_shell_run(
        self,
        *,
        skill_payload: dict[str, Any],
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        command_plan: SkillCommandPlan,
        run_id: str,
        started_at: str,
        workspace_dir: Path,
        request_file: Path,
        context_file: Path,
        generated_files: list[str],
        preflight: dict[str, Any],
        shell_command: str,
        env: dict[str, str],
        skill_env_spec: dict[str, Any] | None,
        timeout_s: int,
        cleanup_workspace: bool,
        worker_started_at: str | None = None,
    ) -> dict[str, Any]:
        current_skill_env_spec = (
            dict(skill_env_spec) if isinstance(skill_env_spec, dict) else None
        )
        try:
            final_plan = command_plan
            final_preflight = preflight
            final_command = shell_command
            final_execution = self.failed_execution_payload(workspace_dir=workspace_dir)
            repair_attempted = False
            repair_succeeded = False
            repaired_from_run_id: str | None = None
            repair_attempt_count = 0
            repair_history: list[dict[str, Any]] = []
            bootstrap_attempted = False
            bootstrap_succeeded = False
            bootstrap_attempt_count = 0
            bootstrap_history: list[dict[str, Any]] = []
            bootstrap_stdout_parts: list[str] = []
            bootstrap_stderr_parts: list[str] = []

            current_plan = command_plan
            current_preflight = preflight
            current_command = shell_command
            current_generated_files = list(generated_files)
            current_bootstrap_pending = bool(current_plan.bootstrap_commands)
            current_env = dict(env)

            def _can_attempt_llm_repair(plan: SkillCommandPlan) -> bool:
                if plan.mode == "explicit":
                    return False
                llm_client = self.get_repair_llm_client()
                return bool(
                    llm_client is not None
                    and llm_client.is_available()
                    and not self.deps.is_cancel_requested(run_id)
                    and repair_attempt_count < self.deps.max_repair_attempts
                )

            while True:
                final_plan = current_plan
                final_preflight = current_preflight
                final_command = current_command
                generated_files = list(current_generated_files)

                try:
                    current_env, current_skill_env_spec = self.deps.build_run_env(
                        skill_dir=loaded_skill.skill.path.resolve(),
                        workspace_dir=workspace_dir,
                        goal=request.goal,
                        user_query=request.user_query,
                        constraints=request.constraints,
                        runtime_target=request.runtime_target,
                        request_file=request_file,
                        context_file=context_file,
                        loaded_skill=loaded_skill,
                        command_plan=current_plan,
                        materialize_skill_env=not is_dry_run(request.constraints),
                    )
                except SkillServerError as exc:
                    if current_skill_env_spec is None and isinstance(skill_env_spec, dict):
                        current_skill_env_spec = dict(skill_env_spec)
                    if isinstance(current_skill_env_spec, dict):
                        current_skill_env_spec["error"] = str(exc)
                    current_preflight = {
                        **dict(current_preflight),
                        "status": "manual_required",
                        "ok": False,
                        "failure_reason": "skill_env_unavailable",
                    }
                    final_preflight = current_preflight
                    final_execution = self.failed_execution_payload(
                        workspace_dir=workspace_dir,
                        stderr=str(exc),
                    )
                    can_repair_env = _can_attempt_llm_repair(current_plan)
                    if not can_repair_env:
                        break

                    snapshot_index = len(repair_history) + 1
                    repair_attempted = True
                    failed_attempt_record = self.deps.skill_run_record_cls(
                        skill_name=request.skill_name,
                        run_status="failed",
                        success=False,
                        summary=(
                            f"Skill environment attempt {snapshot_index} for skill "
                            f"'{request.skill_name}' failed."
                        ),
                        command_plan=current_plan,
                        run_id=run_id,
                        command=current_command,
                        workspace=str(workspace_dir),
                        started_at=started_at,
                        finished_at=self.deps.utc_now(),
                        failure_reason="skill_env_unavailable",
                        artifacts=self.deps.collect_workspace_artifacts(workspace_dir),
                        logs_preview=final_execution["logs_preview"],
                        runtime=self.runtime_metadata_with_skill_env(
                            runtime=self.durable_runtime_metadata(
                                free_shell_repair_limit=self.deps.max_repair_attempts,
                            ),
                            skill_env_spec=current_skill_env_spec,
                        ),
                        preflight=current_preflight,
                        repair_attempted=True,
                        repair_attempt_count=repair_attempt_count,
                        repair_attempt_limit=self.deps.max_repair_attempts,
                        repair_history=list(repair_history),
                        bootstrap_attempted=bootstrap_attempted,
                        bootstrap_succeeded=bootstrap_succeeded,
                        bootstrap_attempt_count=bootstrap_attempt_count,
                        bootstrap_attempt_limit=self.deps.max_bootstrap_attempts,
                        bootstrap_history=list(bootstrap_history),
                    )
                    failed_attempt_payload = self.deps.build_run_record_payload(
                        record=failed_attempt_record,
                        goal=request.goal,
                        user_query=request.user_query,
                        constraints=request.constraints,
                        payload=skill_payload,
                        loaded_skill=loaded_skill,
                    )
                    failed_attempt_payload["request_file"] = str(request_file)
                    failed_attempt_payload["context_file"] = str(context_file)
                    failed_attempt_payload["generated_files"] = list(current_generated_files)
                    failed_attempt_payload["logs"] = {
                        "stdout": "",
                        "stderr": str(exc),
                    }
                    snapshot_run_id = self.store_attempt_snapshot(
                        base_run_id=run_id,
                        suffix=f"attempt-{snapshot_index}",
                        record=failed_attempt_payload,
                    )
                    if repaired_from_run_id is None:
                        repaired_from_run_id = snapshot_run_id
                    repair_history.append(
                        {
                            "attempt_index": snapshot_index,
                            "snapshot_run_id": snapshot_run_id,
                            "stage": "environment",
                            "plan_mode": current_plan.mode,
                            "entrypoint": current_plan.entrypoint,
                            "generated_files": [item.path for item in current_plan.generated_files],
                            "command": current_command,
                            "preflight_status": current_preflight.get("status"),
                            "preflight_failure_reason": current_preflight.get("failure_reason"),
                            "failure_reason": "skill_env_unavailable",
                            "exit_code": None,
                            "started_at": started_at,
                            "finished_at": self.deps.utc_now(),
                            "duration_s": None,
                            "stdout_tail": "",
                            "stderr_tail": str(exc)[-2000:],
                        }
                    )
                    repair_attempt_count += 1

                    try:
                        repaired_plan = await self.deps.attempt_repair_plan(
                            loaded_skill=loaded_skill,
                            request=request,
                            command_plan=current_plan,
                            command=current_command or "",
                            failure_stage="environment",
                            exit_code=None,
                            stdout="",
                            stderr=str(exc),
                            preflight=current_preflight,
                            repair_history=repair_history,
                            llm_client=self.get_repair_llm_client(),
                        )
                    except Exception:
                        repaired_plan = None
                    if repaired_plan is None:
                        break

                    if current_generated_files:
                        self.remove_workspace_files(
                            workspace_dir=workspace_dir,
                            relative_paths=current_generated_files,
                        )
                    current_plan = repaired_plan
                    current_preflight, current_generated_files = self.deps.run_preflight(
                        workspace_dir=workspace_dir,
                        command_plan=current_plan,
                        env=current_env,
                    )
                    current_command = self.deps.materialize_shell_command(
                        loaded_skill=loaded_skill,
                        command_plan=current_plan,
                        workspace_dir=workspace_dir,
                    )
                    current_bootstrap_pending = bool(current_plan.bootstrap_commands)
                    if not current_command:
                        current_preflight = {
                            **dict(current_preflight),
                            "status": "manual_required",
                            "ok": False,
                            "failure_reason": (
                                current_preflight.get("failure_reason")
                                or "command_materialization_failed"
                            ),
                        }
                    continue

                if current_plan.bootstrap_commands and current_bootstrap_pending:
                    if bootstrap_attempt_count >= self.deps.max_bootstrap_attempts:
                        current_preflight = {
                            **dict(current_preflight),
                            "status": "manual_required",
                            "ok": False,
                            "failure_reason": (
                                current_preflight.get("failure_reason")
                                or "bootstrap_attempt_limit_exceeded"
                            ),
                        }
                        current_bootstrap_pending = False
                    else:
                        bootstrap_attempted = True
                        bootstrap_result = await self.execute_bootstrap_commands(
                            commands=current_plan.bootstrap_commands,
                            workspace_dir=workspace_dir,
                            env=current_env,
                            timeout_s=timeout_s,
                        )
                        bootstrap_attempt_count += 1
                        bootstrap_history.append(
                            self.build_bootstrap_history_entry(
                                attempt_index=bootstrap_attempt_count,
                                commands=current_plan.bootstrap_commands,
                                result=bootstrap_result,
                            )
                        )
                        bootstrap_stdout_parts.append(
                            str(bootstrap_result.get("stdout", ""))
                        )
                        bootstrap_stderr_parts.append(
                            str(bootstrap_result.get("stderr", ""))
                        )
                        current_bootstrap_pending = False
                        if bootstrap_result.get("success"):
                            bootstrap_succeeded = True
                            current_preflight, current_generated_files = (
                                self.deps.run_preflight(
                                    workspace_dir=workspace_dir,
                                    command_plan=current_plan,
                                    env=current_env,
                                )
                            )
                            current_command = self.deps.materialize_shell_command(
                                loaded_skill=loaded_skill,
                                command_plan=current_plan,
                                workspace_dir=workspace_dir,
                            )
                            if not current_command:
                                current_preflight = {
                                    **dict(current_preflight),
                                    "status": "manual_required",
                                    "ok": False,
                                    "failure_reason": (
                                        current_preflight.get("failure_reason")
                                        or "command_materialization_failed"
                                    ),
                                }
                            continue
                        current_preflight = {
                            **dict(current_preflight),
                            "status": "manual_required",
                            "ok": False,
                            "failure_reason": (
                                bootstrap_result.get("failure_reason")
                                or current_preflight.get("failure_reason")
                                or "bootstrap_failed"
                            ),
                        }
                        final_execution = self.failed_execution_payload(
                            workspace_dir=workspace_dir,
                            stdout=str(bootstrap_result.get("stdout", "")),
                            stderr=str(bootstrap_result.get("stderr", "")),
                            exit_code=bootstrap_result.get("exit_code"),
                        )

                if not current_preflight.get("ok") or not current_command:
                    failure_reason = (
                        current_preflight.get("failure_reason")
                        or "command_materialization_failed"
                    )
                    final_preflight = {
                        **dict(current_preflight),
                        "status": str(
                            current_preflight.get("status") or "manual_required"
                        ),
                        "ok": False,
                        "failure_reason": failure_reason,
                    }
                    final_execution = self.failed_execution_payload(
                        workspace_dir=workspace_dir
                    )
                    can_repair_preflight = _can_attempt_llm_repair(current_plan)
                    if not can_repair_preflight:
                        break

                    snapshot_index = len(repair_history) + 1
                    repair_attempted = True
                    failed_attempt_record = self.deps.skill_run_record_cls(
                        skill_name=request.skill_name,
                        run_status="failed",
                        success=False,
                        summary=(
                            f"Free-shell preflight attempt {snapshot_index} for skill "
                            f"'{request.skill_name}' failed."
                        ),
                        command_plan=current_plan,
                        run_id=run_id,
                        command=current_command,
                        workspace=str(workspace_dir),
                        started_at=started_at,
                        finished_at=self.deps.utc_now(),
                        failure_reason=failure_reason,
                        artifacts=self.deps.collect_workspace_artifacts(workspace_dir),
                        logs_preview=final_execution["logs_preview"],
                        runtime=self.runtime_metadata_with_skill_env(
                            runtime=self.durable_runtime_metadata(
                                free_shell_repair_limit=self.deps.max_repair_attempts,
                            ),
                            skill_env_spec=current_skill_env_spec,
                        ),
                        preflight=final_preflight,
                        repair_attempted=True,
                        repair_attempt_count=repair_attempt_count,
                        repair_attempt_limit=self.deps.max_repair_attempts,
                        repair_history=list(repair_history),
                        bootstrap_attempted=bootstrap_attempted,
                        bootstrap_succeeded=bootstrap_succeeded,
                        bootstrap_attempt_count=bootstrap_attempt_count,
                        bootstrap_attempt_limit=self.deps.max_bootstrap_attempts,
                        bootstrap_history=list(bootstrap_history),
                    )
                    failed_attempt_payload = self.deps.build_run_record_payload(
                        record=failed_attempt_record,
                        goal=request.goal,
                        user_query=request.user_query,
                        constraints=request.constraints,
                        payload=skill_payload,
                        loaded_skill=loaded_skill,
                    )
                    failed_attempt_payload["request_file"] = str(request_file)
                    failed_attempt_payload["context_file"] = str(context_file)
                    failed_attempt_payload["generated_files"] = list(current_generated_files)
                    failed_attempt_payload["logs"] = {"stdout": "", "stderr": ""}
                    snapshot_run_id = self.store_attempt_snapshot(
                        base_run_id=run_id,
                        suffix=f"attempt-{snapshot_index}",
                        record=failed_attempt_payload,
                    )
                    if repaired_from_run_id is None:
                        repaired_from_run_id = snapshot_run_id
                    repair_history.append(
                        self.build_repair_history_entry(
                            attempt_index=snapshot_index,
                            stage="preflight",
                            snapshot_run_id=snapshot_run_id,
                            command_plan=current_plan,
                            command=current_command,
                            preflight=final_preflight,
                            execution=final_execution,
                        )
                    )
                    repair_attempt_count += 1

                    try:
                        repaired_plan = await self.deps.attempt_repair_plan(
                            loaded_skill=loaded_skill,
                            request=request,
                            command_plan=current_plan,
                            command=current_command,
                            failure_stage="preflight",
                            exit_code=None,
                            stdout="",
                            stderr="",
                            preflight=final_preflight,
                            repair_history=repair_history,
                            llm_client=self.get_repair_llm_client(),
                        )
                    except Exception:
                        repaired_plan = None
                    if repaired_plan is None:
                        break

                    if current_generated_files:
                        self.remove_workspace_files(
                            workspace_dir=workspace_dir,
                            relative_paths=current_generated_files,
                        )
                    current_plan = repaired_plan
                    current_preflight, current_generated_files = self.deps.run_preflight(
                        workspace_dir=workspace_dir,
                        command_plan=current_plan,
                        env=current_env,
                    )
                    current_command = self.deps.materialize_shell_command(
                        loaded_skill=loaded_skill,
                        command_plan=current_plan,
                        workspace_dir=workspace_dir,
                    )
                    current_bootstrap_pending = bool(current_plan.bootstrap_commands)
                    if not current_command:
                        current_preflight = {
                            **dict(current_preflight),
                            "status": "manual_required",
                            "ok": False,
                            "failure_reason": (
                                current_preflight.get("failure_reason")
                                or "command_materialization_failed"
                            ),
                        }
                    continue

                self.deps.update_run_metadata(
                    run_id=run_id,
                    queue_state="executing",
                    worker_pid=os.getpid(),
                    cancel_requested=self.deps.is_cancel_requested(run_id),
                    heartbeat=True,
                )
                execution = await self.execute_shell_command(
                    run_id=run_id,
                    shell_command=current_command,
                    workspace_dir=workspace_dir,
                    env=current_env,
                    timeout_s=timeout_s,
                )
                final_execution = execution
                execution_cancelled = bool(
                    execution.get("cancelled") or execution.get("cancel_requested")
                )
                can_repair_execution = (
                    _can_attempt_llm_repair(current_plan)
                    and not execution["success"]
                    and not execution_cancelled
                )
                if not can_repair_execution:
                    break

                snapshot_index = len(repair_history) + 1
                repair_attempted = True
                failed_attempt_record = self.deps.skill_run_record_cls(
                    skill_name=request.skill_name,
                    run_status="failed",
                    success=False,
                    summary=(
                        f"Free-shell execution attempt {snapshot_index} for skill "
                        f"'{request.skill_name}' failed."
                    ),
                    command_plan=current_plan,
                    run_id=run_id,
                    command=current_command,
                    workspace=str(workspace_dir),
                    started_at=started_at,
                    finished_at=self.deps.utc_now(),
                    exit_code=execution["exit_code"],
                    failure_reason=self.failure_reason_from_execution(execution),
                    artifacts=execution["artifacts"],
                    logs_preview=execution["logs_preview"],
                    runtime=self.runtime_metadata_with_skill_env(
                        runtime=self.durable_runtime_metadata(
                            free_shell_repair_limit=self.deps.max_repair_attempts,
                        ),
                        skill_env_spec=current_skill_env_spec,
                    ),
                    preflight=current_preflight,
                    repair_attempted=True,
                    repair_attempt_count=repair_attempt_count,
                    repair_attempt_limit=self.deps.max_repair_attempts,
                    repair_history=list(repair_history),
                    bootstrap_attempted=bootstrap_attempted,
                    bootstrap_succeeded=bootstrap_succeeded,
                    bootstrap_attempt_count=bootstrap_attempt_count,
                    bootstrap_attempt_limit=self.deps.max_bootstrap_attempts,
                    bootstrap_history=list(bootstrap_history),
                )
                failed_attempt_payload = self.deps.build_run_record_payload(
                    record=failed_attempt_record,
                    goal=request.goal,
                    user_query=request.user_query,
                    constraints=request.constraints,
                    payload=skill_payload,
                    loaded_skill=loaded_skill,
                )
                failed_attempt_payload["request_file"] = str(request_file)
                failed_attempt_payload["context_file"] = str(context_file)
                failed_attempt_payload["generated_files"] = list(current_generated_files)
                failed_attempt_payload["logs"] = {
                    "stdout": execution["stdout"],
                    "stderr": execution["stderr"],
                }
                snapshot_run_id = self.store_attempt_snapshot(
                    base_run_id=run_id,
                    suffix=f"attempt-{snapshot_index}",
                    record=failed_attempt_payload,
                )
                if repaired_from_run_id is None:
                    repaired_from_run_id = snapshot_run_id
                repair_history.append(
                    self.build_repair_history_entry(
                        attempt_index=snapshot_index,
                        stage="execution",
                        snapshot_run_id=snapshot_run_id,
                        command_plan=current_plan,
                        command=current_command,
                        preflight=current_preflight,
                        execution=execution,
                    )
                )
                repair_attempt_count += 1

                try:
                    repaired_plan = await self.deps.attempt_repair_plan(
                        loaded_skill=loaded_skill,
                        request=request,
                        command_plan=current_plan,
                        command=current_command,
                        failure_stage="execution",
                        exit_code=execution["exit_code"],
                        stdout=execution["stdout"],
                        stderr=execution["stderr"],
                        preflight=current_preflight,
                        repair_history=repair_history,
                        llm_client=self.get_repair_llm_client(),
                    )
                except Exception:
                    repaired_plan = None
                if repaired_plan is None:
                    break

                if current_generated_files:
                    self.remove_workspace_files(
                        workspace_dir=workspace_dir,
                        relative_paths=current_generated_files,
                    )
                current_plan = repaired_plan
                current_preflight, current_generated_files = self.deps.run_preflight(
                    workspace_dir=workspace_dir,
                    command_plan=current_plan,
                    env=current_env,
                )
                current_command = self.deps.materialize_shell_command(
                    loaded_skill=loaded_skill,
                    command_plan=current_plan,
                    workspace_dir=workspace_dir,
                )
                current_bootstrap_pending = bool(current_plan.bootstrap_commands)
                if not current_command:
                    current_preflight = {
                        **dict(current_preflight),
                        "status": "manual_required",
                        "ok": False,
                        "failure_reason": (
                            current_preflight.get("failure_reason")
                            or "command_materialization_failed"
                        ),
                    }

            bootstrap_stdout = "".join(bootstrap_stdout_parts)
            bootstrap_stderr = "".join(bootstrap_stderr_parts)
            if bootstrap_stdout or bootstrap_stderr:
                combined_stdout = bootstrap_stdout + str(final_execution.get("stdout", ""))
                combined_stderr = bootstrap_stderr + str(final_execution.get("stderr", ""))
                stdout_preview, stdout_truncated = self.deps.truncate_log(combined_stdout)
                stderr_preview, stderr_truncated = self.deps.truncate_log(combined_stderr)
                final_execution = {
                    **dict(final_execution),
                    "stdout": combined_stdout,
                    "stderr": combined_stderr,
                    "logs_preview": {
                        "stdout": stdout_preview,
                        "stderr": stderr_preview,
                        "stdout_truncated": stdout_truncated,
                        "stderr_truncated": stderr_truncated,
                    },
                }

            repair_succeeded = repair_attempted and bool(final_execution.get("success"))

            final_cancelled = bool(
                final_execution.get("cancelled")
                or final_execution.get("cancel_requested")
            )
            final_success = bool(final_execution["success"])
            final_run_status = "completed" if final_success else "failed"
            finished_at = self.deps.utc_now()
            final_failure_reason = None
            if not final_success:
                if final_preflight.get("failure_reason"):
                    final_failure_reason = final_preflight["failure_reason"]
                elif final_cancelled:
                    final_failure_reason = "cancelled"
                elif final_execution.get("timed_out"):
                    final_failure_reason = "timed_out"
                else:
                    final_failure_reason = "process_failed"

            final_record = self.deps.skill_run_record_cls(
                skill_name=request.skill_name,
                run_status=final_run_status,
                success=final_success,
                summary=(
                    f"Executed shell command for skill '{request.skill_name}'."
                    if final_success
                    else (
                        f"Shell execution for skill '{request.skill_name}' was cancelled."
                        if final_cancelled
                        else (
                            f"Shell execution for skill '{request.skill_name}' failed after "
                            f"{repair_attempt_count} repair attempt(s)."
                            if repair_attempted
                            else f"Shell execution for skill '{request.skill_name}' failed."
                        )
                    )
                ),
                command_plan=final_plan,
                run_id=run_id,
                command=final_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=finished_at,
                exit_code=final_execution["exit_code"],
                failure_reason=final_failure_reason,
                artifacts=final_execution["artifacts"],
                logs_preview=final_execution["logs_preview"],
                runtime=self.runtime_metadata_with_skill_env(
                    runtime=self.durable_runtime_metadata(
                        free_shell_repair_limit=self.deps.max_repair_attempts,
                        free_shell_repair_attempts=repair_attempt_count,
                        free_shell_bootstrap_limit=self.deps.max_bootstrap_attempts,
                        free_shell_bootstrap_attempts=bootstrap_attempt_count,
                        enqueued_at=started_at,
                        worker_started_at=worker_started_at,
                        queue_latency_s=utc_duration_seconds(started_at, worker_started_at),
                        bootstrap_duration_s=sum(
                            float(item.get("duration_s", 0.0) or 0.0)
                            for item in bootstrap_history
                            if isinstance(item, dict)
                        ),
                        execution_duration_s=final_execution.get("duration_s"),
                        total_duration_s=utc_duration_seconds(started_at, finished_at),
                    ),
                    skill_env_spec=current_skill_env_spec,
                ),
                preflight=final_preflight,
                repair_attempted=repair_attempted,
                repair_succeeded=repair_succeeded,
                repaired_from_run_id=repaired_from_run_id,
                repair_attempt_count=repair_attempt_count,
                repair_attempt_limit=self.deps.max_repair_attempts,
                repair_history=repair_history,
                bootstrap_attempted=bootstrap_attempted,
                bootstrap_succeeded=bootstrap_succeeded,
                bootstrap_attempt_count=bootstrap_attempt_count,
                bootstrap_attempt_limit=self.deps.max_bootstrap_attempts,
                bootstrap_history=bootstrap_history,
                cancel_requested=bool(final_execution.get("cancel_requested", False)),
            )
            response = self.deps.build_run_record_payload(
                record=final_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            response["request_file"] = str(request_file)
            response["context_file"] = str(context_file)
            response["generated_files"] = generated_files
            response["timed_out"] = bool(final_execution.get("timed_out", False))
            response["logs"] = {
                "stdout": final_execution["stdout"],
                "stderr": final_execution["stderr"],
            }
            try:
                self.deps.write_terminal_snapshot(
                    workspace_dir=workspace_dir,
                    record=response,
                )
            except Exception:
                pass
            self.store_run_record(response)
            self.deps.update_run_metadata(
                run_id=run_id,
                queue_state=final_run_status,
                worker_pid=os.getpid(),
                active_process_pid=None,
                cancel_requested=bool(final_execution.get("cancel_requested", False)),
                heartbeat=True,
            )
            return self.deps.prepare_transport_payload(response)
        except Exception as exc:
            failed_record = self.deps.skill_run_record_cls(
                skill_name=request.skill_name,
                run_status="failed",
                success=False,
                summary=f"Skill runtime crashed while executing '{request.skill_name}'.",
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=self.deps.utc_now(),
                failure_reason="runtime_internal_error",
                artifacts=self.deps.collect_workspace_artifacts(workspace_dir),
                runtime=self.runtime_metadata_with_skill_env(
                    runtime=self.durable_runtime_metadata(error=str(exc)),
                    skill_env_spec=current_skill_env_spec,
                ),
                preflight=preflight,
                cancel_requested=self.deps.is_cancel_requested(run_id),
            )
            response = self.deps.build_run_record_payload(
                record=failed_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            response["request_file"] = str(request_file)
            response["context_file"] = str(context_file)
            response["generated_files"] = generated_files
            response["logs"] = {"stdout": "", "stderr": str(exc)}
            try:
                self.deps.write_terminal_snapshot(
                    workspace_dir=workspace_dir,
                    record=response,
                )
            except Exception:
                pass
            self.store_run_record(response)
            self.deps.update_run_metadata(
                run_id=run_id,
                queue_state="failed",
                worker_pid=os.getpid(),
                active_process_pid=None,
                cancel_requested=self.deps.is_cancel_requested(run_id),
                heartbeat=True,
            )
            return self.deps.prepare_transport_payload(response)
        finally:
            if cleanup_workspace:
                shutil.rmtree(workspace_dir, ignore_errors=True)

    def build_worker_job_context(
        self,
        *,
        request: SkillExecutionRequest,
        command_plan: SkillCommandPlan,
        run_id: str,
        started_at: str,
        workspace_dir: Path,
        request_file: Path,
        context_file: Path,
        generated_files: list[str],
        preflight: dict[str, Any],
        shell_command: str,
        timeout_s: int,
        cleanup_workspace: bool,
    ) -> dict[str, Any]:
        utility_llm_stub_payloads: list[dict[str, Any]] = []
        raw_stub_payloads = getattr(self.deps.get_llm_client(), "payloads", None)
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
            "context_file": str(context_file),
            "generated_files": list(generated_files),
            "preflight": dict(preflight),
            "shell_command": shell_command,
            "timeout_s": int(timeout_s),
            "cleanup_workspace": bool(cleanup_workspace),
            "utility_llm_stub_payloads": utility_llm_stub_payloads,
        }

    async def wait_for_terminal_run_record(
        self,
        *,
        run_id: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        self.deps.call_ensure_queue_worker_processes()
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_s)
        while True:
            record = self.deps.call_recover_stale_running_record(run_id)
            if record is None:
                raise SkillServerError(f"Unknown run_id: {run_id}")
            if str(record.get("run_status", "")).strip() != "running":
                return record
            if asyncio.get_running_loop().time() >= deadline:
                return record
            await asyncio.sleep(max(0.05, self.deps.worker_terminal_poll_interval_s))

    async def run_shell_task(
        self,
        *,
        skill_payload: dict[str, Any],
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        command_plan: SkillCommandPlan,
        timeout_s: int,
        cleanup_workspace: bool,
        wait_for_completion: bool,
    ) -> dict[str, Any]:
        self.deps.runs_root.mkdir(parents=True, exist_ok=True)
        workspace_dir = Path(
            tempfile.mkdtemp(prefix=f"{skill_payload['name']}-", dir=str(self.deps.runs_root))
        ).resolve()
        run_id = f"skill-run-{uuid.uuid4().hex}"
        started_at = self.deps.utc_now()
        request_file: Path | None = None
        context_file: Path | None = None
        effective_runtime_target = command_plan.runtime_target

        cleanup_workspace_here = cleanup_workspace
        try:
            mirrored_skill_files = self.deps.materialize_skill_workspace_view(
                loaded_skill=loaded_skill,
                workspace_dir=workspace_dir,
            )
            self.deps.write_mirrored_skill_manifest(
                workspace_dir=workspace_dir,
                mirrored_paths=mirrored_skill_files,
            )
            request_file = self.deps.write_skill_request(
                workspace_dir=workspace_dir,
                skill_payload=skill_payload,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                workspace=request.workspace,
                runtime_target=effective_runtime_target.to_dict(),
                command_plan=command_plan.to_dict(),
            )
            context_file = self.deps.write_skill_context(
                workspace_dir=workspace_dir,
                skill_payload=skill_payload,
            )
            initial_skill_env_error: str | None = None
            try:
                env, skill_env_spec = self.deps.build_run_env(
                    skill_dir=Path(skill_payload["path"]).resolve(),
                    workspace_dir=workspace_dir,
                    goal=request.goal,
                    user_query=request.user_query,
                    constraints=request.constraints,
                    runtime_target=effective_runtime_target,
                    request_file=request_file,
                    context_file=context_file,
                    loaded_skill=loaded_skill,
                    command_plan=command_plan,
                    materialize_skill_env=False,
                )
                preflight, generated_files = self.deps.run_preflight(
                    workspace_dir=workspace_dir,
                    command_plan=command_plan,
                    env=env,
                )
            except SkillServerError as exc:
                initial_skill_env_error = str(exc)
                env = os.environ.copy()
                skill_env_spec = {"error": initial_skill_env_error}
                preflight = {
                    "status": "manual_required",
                    "ok": False,
                    "failure_reason": "skill_env_unavailable",
                    "required_tools": [],
                    "generated_files": [],
                }
                generated_files = []
            shell_command = self.deps.materialize_shell_command(
                loaded_skill=loaded_skill,
                command_plan=command_plan,
                workspace_dir=workspace_dir,
            )
            if not shell_command:
                preflight = {
                    **dict(preflight),
                    "status": "manual_required",
                    "ok": False,
                    "failure_reason": preflight.get("failure_reason")
                    or "command_materialization_failed",
                }
                shell_command = ""

            can_auto_repair_preflight = (
                not is_dry_run(request.constraints)
                and command_plan.mode != "explicit"
                and self.deps.get_llm_client().is_available()
            )
            can_auto_bootstrap_preflight = (
                not is_dry_run(request.constraints)
                and bool(command_plan.bootstrap_commands)
            )
            if not preflight["ok"] and not (
                can_auto_repair_preflight or can_auto_bootstrap_preflight
            ):
                preflight_record = self.deps.skill_run_record_cls(
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
                    finished_at=self.deps.utc_now(),
                    failure_reason=preflight.get("failure_reason"),
                    runtime=self.runtime_metadata_with_skill_env(
                        runtime={"executor": "shell"},
                        skill_env_spec=skill_env_spec,
                    ),
                    preflight=preflight,
                )
                response = self.deps.build_run_record_payload(
                    record=preflight_record,
                    goal=request.goal,
                    user_query=request.user_query,
                    constraints=request.constraints,
                    payload=skill_payload,
                    loaded_skill=loaded_skill,
                )
                response["request_file"] = str(request_file)
                response["context_file"] = str(context_file)
                response["generated_files"] = generated_files
                response["mirrored_skill_files"] = mirrored_skill_files
                response["logs"] = {
                    "stdout": "",
                    "stderr": initial_skill_env_error or "",
                }
                self.store_run_record(response)
                return self.deps.prepare_transport_payload(response)

            running_record = self.deps.skill_run_record_cls(
                skill_name=request.skill_name,
                run_status="running",
                success=True,
                summary=(
                    f"Running free-shell repair loop for skill '{request.skill_name}'."
                    if not preflight["ok"]
                    else f"Running shell task for skill '{request.skill_name}'."
                ),
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                runtime=self.durable_runtime_metadata(
                    queue_state="queued",
                    attempt_count=0,
                    max_attempts=self.deps.queue_max_attempts,
                    free_shell_repair_limit=self.deps.max_repair_attempts,
                    free_shell_repair_attempts=0,
                    free_shell_bootstrap_limit=self.deps.max_bootstrap_attempts,
                    free_shell_bootstrap_attempts=0,
                    enqueued_at=started_at,
                ),
                preflight=preflight,
                repair_attempt_count=0,
                repair_attempt_limit=self.deps.max_repair_attempts,
                bootstrap_attempt_count=0,
                bootstrap_attempt_limit=self.deps.max_bootstrap_attempts,
                cancel_requested=False,
            )
            running_payload = self.deps.build_run_record_payload(
                record=running_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=skill_payload,
                loaded_skill=loaded_skill,
            )
            running_payload["runtime"] = self.runtime_metadata_with_skill_env(
                runtime=running_payload.get("runtime")
                if isinstance(running_payload.get("runtime"), dict)
                else {},
                skill_env_spec=skill_env_spec,
            )
            running_payload["request_file"] = str(request_file)
            running_payload["context_file"] = str(context_file)
            running_payload["generated_files"] = generated_files
            running_payload["mirrored_skill_files"] = mirrored_skill_files
            running_payload["logs"] = {
                "stdout": "",
                "stderr": initial_skill_env_error or "",
            }
            self.store_run_record(running_payload)
            job_context = self.build_worker_job_context(
                request=request,
                command_plan=command_plan,
                run_id=run_id,
                started_at=started_at,
                workspace_dir=workspace_dir,
                request_file=request_file,
                context_file=context_file,
                generated_files=generated_files,
                preflight=preflight,
                shell_command=shell_command,
                timeout_s=timeout_s,
                cleanup_workspace=cleanup_workspace,
            )
            self.deps.upsert_run_row(
                run_id=run_id,
                record=running_payload,
                job_context=job_context,
                queue_state="queued",
                cancel_requested=False,
                heartbeat=False,
            )
            cleanup_workspace_here = False
            try:
                worker_pids = self.deps.call_ensure_queue_worker_processes()
            except Exception as exc:
                failed_record = self.deps.skill_run_record_cls(
                    skill_name=request.skill_name,
                    run_status="failed",
                    success=False,
                    summary=(
                        f"Failed to start durable worker for skill '{request.skill_name}'."
                    ),
                    command_plan=command_plan,
                    run_id=run_id,
                    command=shell_command,
                    workspace=str(workspace_dir),
                    started_at=started_at,
                    finished_at=self.deps.utc_now(),
                    failure_reason="worker_start_failed",
                    runtime=self.runtime_metadata_with_skill_env(
                        runtime=self.durable_runtime_metadata(
                            queue_state="failed",
                            error=str(exc),
                        ),
                        skill_env_spec=skill_env_spec,
                    ),
                    preflight=preflight,
                )
                failed_payload = self.deps.build_run_record_payload(
                    record=failed_record,
                    goal=request.goal,
                    user_query=request.user_query,
                    constraints=request.constraints,
                    payload=skill_payload,
                    loaded_skill=loaded_skill,
                )
                failed_payload["request_file"] = str(request_file)
                failed_payload["context_file"] = str(context_file)
                failed_payload["generated_files"] = generated_files
                failed_payload["mirrored_skill_files"] = mirrored_skill_files
                failed_payload["logs"] = {"stdout": "", "stderr": ""}
                self.store_run_record(failed_payload)
                self.deps.update_run_metadata(
                    run_id=run_id,
                    queue_state="failed",
                    worker_pid=None,
                    active_process_pid=None,
                    cancel_requested=False,
                    heartbeat=True,
                )
                return self.deps.prepare_transport_payload(failed_payload)
            if wait_for_completion:
                terminal_record = await self.wait_for_terminal_run_record(
                    run_id=run_id,
                    timeout_s=float(timeout_s) + self.deps.wait_for_terminal_grace_s,
                )
                return self.deps.prepare_transport_payload(terminal_record)
            handoff_deadline = (
                asyncio.get_running_loop().time()
                + max(0.5, self.deps.queue_worker_startup_wait_s * 2)
            )
            latest_queue_state = "queued"
            while asyncio.get_running_loop().time() < handoff_deadline:
                latest_record = self.deps.load_run_record(run_id)
                if not isinstance(latest_record, dict):
                    break
                if str(latest_record.get("run_status", "")).strip() != "running":
                    return self.deps.prepare_transport_payload(latest_record)
                latest_runtime = (
                    latest_record.get("runtime")
                    if isinstance(latest_record.get("runtime"), dict)
                    else {}
                )
                latest_queue_state = str(latest_runtime.get("queue_state", "")).strip()
                if latest_queue_state and latest_queue_state != "queued":
                    break
                await asyncio.sleep(max(0.05, self.deps.queue_worker_poll_interval_s))
            running_payload["notes"] = (
                "Shell task was enqueued for a durable worker. Poll get_run_status/get_run_logs/"
                "get_run_artifacts with run_id for live progress or terminal results. "
                "Use cancel_skill_run to stop it."
            )
            running_payload["runtime"] = dict(running_payload.get("runtime", {}))
            running_payload["runtime"]["queue_state"] = latest_queue_state or "queued"
            if worker_pids:
                running_payload["runtime"]["queue_worker_pids"] = worker_pids
            return self.deps.prepare_transport_payload(running_payload)
        finally:
            if cleanup_workspace_here:
                shutil.rmtree(workspace_dir, ignore_errors=True)

    async def run_durable_worker(self, run_id: str) -> int:
        worker_started_at = self.deps.utc_now()
        job_context = self.deps.load_run_job_context(run_id)
        if not isinstance(job_context, dict):
            raise SkillServerError(f"Missing worker job context for run_id: {run_id}")

        current_record = self.deps.load_run_record(run_id)
        if current_record is None:
            raise SkillServerError(f"Unknown run_id: {run_id}")
        if str(current_record.get("run_status", "")).strip() != "running":
            return 0
        self.deps.update_run_metadata(
            run_id=run_id,
            queue_state="worker_starting",
            worker_pid=os.getpid(),
            active_process_pid=None,
            cancel_requested=bool(current_record.get("cancel_requested", False)),
            heartbeat=True,
        )

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
        started_at = str(job_context.get("started_at") or self.deps.utc_now())
        workspace_dir = Path(str(job_context.get("workspace_dir", ""))).resolve()
        request_file = Path(str(job_context.get("request_file", ""))).resolve()
        context_file = Path(str(job_context.get("context_file", ""))).resolve()
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
        timeout_s = int(job_context.get("timeout_s", self.deps.default_run_timeout_s))
        cleanup_workspace = bool(job_context.get("cleanup_workspace", False))
        stub_payloads = (
            job_context.get("utility_llm_stub_payloads")
            if isinstance(job_context.get("utility_llm_stub_payloads"), list)
            else []
        )
        if stub_payloads:
            stub = self.deps.serialized_utility_llm_stub_cls(
                [dict(item) for item in stub_payloads if isinstance(item, dict)]
            )
            self.deps.set_llm_client(stub)

        payload = self.deps.load_skill_payload(skill_name)
        loaded_skill = self.deps.build_loaded_skill(skill_name)
        request = SkillExecutionRequest(
            skill_name=skill_name,
            goal=goal,
            user_query=user_query,
            workspace=workspace,
            runtime_target=runtime_target,
            constraints=constraints,
        )

        if self.deps.is_cancel_requested(run_id):
            cancelled_record = self.deps.skill_run_record_cls(
                skill_name=request.skill_name,
                run_status="failed",
                success=False,
                summary=(
                    f"Shell execution for skill '{request.skill_name}' was cancelled before start."
                ),
                command_plan=command_plan,
                run_id=run_id,
                command=shell_command,
                workspace=str(workspace_dir),
                started_at=started_at,
                finished_at=self.deps.utc_now(),
                exit_code=130,
                failure_reason="cancelled",
                artifacts=self.deps.collect_workspace_artifacts(workspace_dir),
                runtime=self.durable_runtime_metadata(queue_state="failed"),
                preflight=preflight,
                cancel_requested=True,
            )
            cancelled_payload = self.deps.build_run_record_payload(
                record=cancelled_record,
                goal=request.goal,
                user_query=request.user_query,
                constraints=request.constraints,
                payload=payload,
                loaded_skill=loaded_skill,
            )
            cancelled_payload["request_file"] = str(request_file)
            cancelled_payload["context_file"] = str(context_file)
            cancelled_payload["generated_files"] = generated_files
            cancelled_payload["logs"] = {"stdout": "", "stderr": ""}
            try:
                self.deps.write_terminal_snapshot(
                    workspace_dir=workspace_dir,
                    record=cancelled_payload,
                )
            except Exception:
                pass
            self.store_run_record(cancelled_payload)
            self.deps.update_run_metadata(
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

        env, skill_env_spec = self.deps.build_run_env(
            skill_dir=Path(payload["path"]).resolve(),
            workspace_dir=workspace_dir,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
            runtime_target=runtime_target,
            request_file=request_file,
            context_file=context_file,
            loaded_skill=loaded_skill,
            command_plan=command_plan,
            materialize_skill_env=False,
        )
        await self.complete_shell_run(
            skill_payload=payload,
            loaded_skill=loaded_skill,
            request=request,
            command_plan=command_plan,
            run_id=run_id,
            started_at=started_at,
            workspace_dir=workspace_dir,
            request_file=request_file,
            context_file=context_file,
            generated_files=generated_files,
            preflight=preflight,
            shell_command=shell_command,
            env=env,
            skill_env_spec=skill_env_spec,
            timeout_s=timeout_s,
            cleanup_workspace=cleanup_workspace,
            worker_started_at=worker_started_at,
        )
        return 0

    async def run_queue_worker_loop(self) -> int:
        idle_started_at = datetime.now(timezone.utc)
        while True:
            claimed_run_id = self.deps.call_claim_next_queued_run()
            if not claimed_run_id:
                idle_age_s = (datetime.now(timezone.utc) - idle_started_at).total_seconds()
                if idle_age_s >= max(2.0, self.deps.queue_worker_startup_wait_s * 2):
                    return 0
                await asyncio.sleep(max(0.1, self.deps.queue_worker_poll_interval_s))
                continue
            idle_started_at = datetime.now(timezone.utc)
            try:
                await self.deps.call_run_durable_worker(claimed_run_id)
            except Exception as exc:
                record = self.deps.load_run_record(claimed_run_id)
                if record is not None and str(record.get("run_status", "")).strip() == "running":
                    record["run_status"] = "failed"
                    record["status"] = "failed"
                    record["success"] = False
                    record["finished_at"] = record.get("finished_at") or self.deps.utc_now()
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
                    workspace = str(record.get("workspace", "")).strip()
                    if workspace:
                        try:
                            self.deps.write_terminal_snapshot(
                                workspace_dir=Path(workspace).resolve(),
                                record=record,
                            )
                        except Exception:
                            pass
                    self.deps.upsert_run_row(
                        run_id=claimed_run_id,
                        record=record,
                        queue_state="failed",
                        worker_pid=os.getpid(),
                        active_process_pid=None,
                        cancel_requested=bool(record.get("cancel_requested", False)),
                        heartbeat=True,
                    )
