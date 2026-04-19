from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping


SkillRuntimePlatform = Literal["linux", "windows"]
SkillRuntimeShell = Literal["/bin/sh", "bash", "powershell"]
SkillShellMode = Literal[
    "conservative",
    "free_shell",
]
SkillRunStatus = Literal[
    "planned",
    "running",
    "completed",
    "failed",
    "manual_required",
]
SkillCommandMode = Literal[
    "explicit",
    "declared_example",
    "structured_args",
    "inferred",
    "free_shell",
    "generated_script",
    "manual_required",
]


def normalize_runtime_platform(value: Any) -> SkillRuntimePlatform:
    normalized = str(value or "").strip().lower()
    if normalized in {"win", "win32", "windows", "powershell"}:
        return "windows"
    return "linux"


def normalize_runtime_shell(
    value: Any,
    *,
    platform: SkillRuntimePlatform | None = None,
) -> SkillRuntimeShell:
    normalized = str(value or "").strip().lower()
    if normalized in {"powershell", "pwsh", "ps", "ps1"}:
        return "powershell"
    if normalized in {"bash", "/bin/bash"}:
        return "bash"
    if normalized in {"sh", "shell", "/bin/sh"}:
        return "/bin/sh"
    if platform == "windows":
        return "powershell"
    return "/bin/sh"


@dataclass(frozen=True)
class SkillRuntimeTarget:
    platform: SkillRuntimePlatform = "linux"
    shell: SkillRuntimeShell = "/bin/sh"
    workspace_root: str = "/workspace"
    workdir: str = "/workspace"
    network_allowed: bool = False
    supports_python: bool = True

    @classmethod
    def linux_default(cls) -> "SkillRuntimeTarget":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "shell": self.shell,
            "workspace_root": self.workspace_root,
            "workdir": self.workdir,
            "network_allowed": self.network_allowed,
            "supports_python": self.supports_python,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        default: "SkillRuntimeTarget | None" = None,
    ) -> "SkillRuntimeTarget":
        fallback = default or cls.linux_default()
        data = dict(payload or {})
        platform = normalize_runtime_platform(data.get("platform", fallback.platform))
        shell = normalize_runtime_shell(
            data.get("shell", fallback.shell),
            platform=platform,
        )
        workspace_root = str(
            data.get("workspace_root", fallback.workspace_root) or fallback.workspace_root
        ).strip() or fallback.workspace_root
        workdir = str(data.get("workdir", fallback.workdir) or fallback.workdir).strip()
        if not workdir:
            workdir = workspace_root
        return cls(
            platform=platform,
            shell=shell,
            workspace_root=workspace_root,
            workdir=workdir,
            network_allowed=bool(
                data.get("network_allowed", fallback.network_allowed)
            ),
            supports_python=bool(
                data.get("supports_python", fallback.supports_python)
            ),
        )


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    path: Path
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_catalog_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "path": str(self.path),
        }


@dataclass(frozen=True)
class SkillPlan:
    skill_name: str
    goal: str
    reason: str
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillFileEntry:
    path: str
    kind: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class LoadedSkill:
    skill: SkillDefinition
    skill_md: str
    file_inventory: list[SkillFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill.to_catalog_dict(),
            "skill_md": self.skill_md,
            "file_inventory": [item.to_dict() for item in self.file_inventory],
        }


@dataclass(frozen=True)
class SkillExecutionRequest:
    skill_name: str
    goal: str
    user_query: str
    workspace: str | None = None
    shell_mode: SkillShellMode = "conservative"
    runtime_target: SkillRuntimeTarget = field(
        default_factory=SkillRuntimeTarget.linux_default
    )
    constraints: dict[str, Any] = field(default_factory=dict)


def normalize_run_status(
    value: Any,
    *,
    success: bool | None = None,
) -> SkillRunStatus:
    normalized = str(value or "").strip().lower()
    if normalized == "needs_shell_command":
        return "manual_required"
    if normalized == "timed_out":
        return "failed"
    if normalized in {
        "planned",
        "running",
        "completed",
        "failed",
        "manual_required",
    }:
        return normalized  # type: ignore[return-value]
    if success is True:
        return "completed"
    return "failed"


def legacy_status_for_run_status(run_status: SkillRunStatus) -> str:
    if run_status == "manual_required":
        return "needs_shell_command"
    return run_status


def normalize_shell_mode(value: Any) -> SkillShellMode:
    normalized = str(value or "").strip().lower()
    if normalized in {"free", "free-shell", "free_shell", "shell_agent", "shell-agent"}:
        return "free_shell"
    return "conservative"


@dataclass(frozen=True)
class SkillGeneratedFile:
    path: str
    content: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "SkillGeneratedFile | None":
        data = dict(payload or {})
        path = str(data.get("path", "")).strip()
        content = str(data.get("content", ""))
        if not path or not content:
            return None
        return cls(
            path=path,
            content=content,
            description=str(data.get("description", "")).strip(),
        )


@dataclass(frozen=True)
class SkillCommandPlan:
    skill_name: str
    goal: str
    user_query: str
    runtime_target: SkillRuntimeTarget = field(
        default_factory=SkillRuntimeTarget.linux_default
    )
    constraints: dict[str, Any] = field(default_factory=dict)
    command: str | None = None
    mode: SkillCommandMode = "manual_required"
    shell_mode: SkillShellMode = "conservative"
    rationale: str = ""
    entrypoint: str | None = None
    cli_args: list[str] = field(default_factory=list)
    generated_files: list[SkillGeneratedFile] = field(default_factory=list)
    bootstrap_commands: list[str] = field(default_factory=list)
    bootstrap_reason: str = ""
    missing_fields: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    hints: dict[str, Any] = field(default_factory=dict)

    @property
    def is_manual_required(self) -> bool:
        return self.mode == "manual_required"

    @property
    def uses_generated_files(self) -> bool:
        return bool(self.generated_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "goal": self.goal,
            "user_query": self.user_query,
            "runtime_target": self.runtime_target.to_dict(),
            "constraints": dict(self.constraints),
            "command": self.command,
            "mode": self.mode,
            "shell_mode": self.shell_mode,
            "rationale": self.rationale,
            "entrypoint": self.entrypoint,
            "cli_args": list(self.cli_args),
            "generated_files": [item.to_dict() for item in self.generated_files],
            "bootstrap_commands": list(self.bootstrap_commands),
            "bootstrap_reason": self.bootstrap_reason,
            "missing_fields": list(self.missing_fields),
            "failure_reason": self.failure_reason,
            "hints": dict(self.hints),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        skill_name: str = "",
        goal: str = "",
        user_query: str = "",
        runtime_target: SkillRuntimeTarget | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> "SkillCommandPlan":
        data = dict(payload or {})
        mode_value = str(data.get("mode", "")).strip().lower()
        if mode_value not in {
            "explicit",
            "declared_example",
            "structured_args",
            "inferred",
            "free_shell",
            "generated_script",
            "manual_required",
        }:
            mode_value = "manual_required"
        plan_constraints = (
            data.get("constraints")
            if isinstance(data.get("constraints"), dict)
            else dict(constraints or {})
        )
        resolved_target = SkillRuntimeTarget.from_dict(
            data.get("runtime_target"),
            default=runtime_target,
        )
        shell_mode = normalize_shell_mode(
            data.get("shell_mode", plan_constraints.get("shell_mode"))
        )
        cli_args = data.get("cli_args")
        generated_files = data.get("generated_files")
        bootstrap_commands = data.get("bootstrap_commands")
        missing_fields = data.get("missing_fields")
        hints = data.get("hints")
        return cls(
            skill_name=str(data.get("skill_name") or skill_name).strip(),
            goal=str(data.get("goal") or goal).strip(),
            user_query=str(data.get("user_query") or user_query).strip(),
            runtime_target=resolved_target,
            constraints=dict(plan_constraints),
            command=(
                str(data.get("command")).strip()
                if isinstance(data.get("command"), str) and data.get("command", "").strip()
                else None
            ),
            mode=mode_value,  # type: ignore[arg-type]
            shell_mode=shell_mode,
            rationale=str(data.get("rationale", "")).strip(),
            entrypoint=(
                str(data.get("entrypoint")).strip()
                if isinstance(data.get("entrypoint"), str)
                and data.get("entrypoint", "").strip()
                else None
            ),
            cli_args=[
                str(item)
                for item in (cli_args if isinstance(cli_args, list) else [])
                if isinstance(item, (str, int, float))
            ],
            generated_files=[
                item
                for item in (
                    SkillGeneratedFile.from_dict(raw)
                    for raw in (generated_files if isinstance(generated_files, list) else [])
                )
                if item is not None
            ],
            bootstrap_commands=[
                str(item).strip()
                for item in (
                    bootstrap_commands if isinstance(bootstrap_commands, list) else []
                )
                if isinstance(item, (str, int, float)) and str(item).strip()
            ],
            bootstrap_reason=str(data.get("bootstrap_reason", "")).strip(),
            missing_fields=[
                str(item)
                for item in (missing_fields if isinstance(missing_fields, list) else [])
                if isinstance(item, (str, int, float))
            ],
            failure_reason=(
                str(data.get("failure_reason")).strip()
                if isinstance(data.get("failure_reason"), str)
                and data.get("failure_reason", "").strip()
                else None
            ),
            hints=dict(hints) if isinstance(hints, dict) else {},
        )


@dataclass(frozen=True)
class SkillRunRecord:
    skill_name: str
    run_status: SkillRunStatus
    success: bool
    summary: str
    command_plan: SkillCommandPlan
    run_id: str | None = None
    command: str | None = None
    workspace: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_previews: list[dict[str, Any]] = field(default_factory=list)
    logs_preview: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    runtime_result: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "shell"
    preflight: dict[str, Any] = field(default_factory=dict)
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repaired_from_run_id: str | None = None
    repair_attempt_count: int = 0
    repair_attempt_limit: int = 0
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    bootstrap_attempted: bool = False
    bootstrap_succeeded: bool = False
    bootstrap_attempt_count: int = 0
    bootstrap_attempt_limit: int = 0
    bootstrap_history: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        hints = dict(self.command_plan.hints)
        shell_mode_requested = normalize_shell_mode(
            hints.get(
                "shell_mode_requested",
                self.command_plan.constraints.get("shell_mode", self.command_plan.shell_mode),
            )
        )
        shell_mode_effective = normalize_shell_mode(
            hints.get("shell_mode_effective", self.command_plan.shell_mode)
        )
        shell_mode_escalated = bool(
            hints.get("shell_mode_escalated", shell_mode_requested != shell_mode_effective)
        )
        payload = {
            "run_id": self.run_id,
            "skill_name": self.skill_name,
            "run_status": self.run_status,
            "status": legacy_status_for_run_status(self.run_status),
            "success": self.success,
            "summary": self.summary,
            "command": self.command,
            "command_plan": self.command_plan.to_dict(),
            "planning_mode": self.command_plan.mode,
            "shell_mode": self.command_plan.shell_mode,
            "shell_mode_requested": shell_mode_requested,
            "shell_mode_effective": shell_mode_effective,
            "shell_mode_escalated": shell_mode_escalated,
            "shell_mode_escalation_reason": hints.get("shell_mode_escalation_reason"),
            "auto_inferred_constraints": dict(hints.get("auto_inferred_constraints", {}))
            if isinstance(hints.get("auto_inferred_constraints"), dict)
            else {},
            "planning_blockers": [
                dict(item)
                for item in hints.get("planning_blockers", [])
                if isinstance(item, dict)
            ]
            if isinstance(hints.get("planning_blockers"), list)
            else [],
            "runtime_target": self.command_plan.runtime_target.to_dict(),
            "workspace": self.workspace,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "failure_reason": self.failure_reason,
            "artifacts": [dict(item) for item in self.artifacts],
            "artifact_previews": [dict(item) for item in self.artifact_previews],
            "logs_preview": dict(self.logs_preview),
            "runtime": dict(self.runtime),
            "runtime_result": dict(self.runtime_result),
            "execution_mode": self.execution_mode,
            "preflight": dict(self.preflight),
            "repair_attempted": self.repair_attempted,
            "repair_succeeded": self.repair_succeeded,
            "repaired_from_run_id": self.repaired_from_run_id,
            "repair_attempt_count": self.repair_attempt_count,
            "repair_attempt_limit": self.repair_attempt_limit,
            "repair_history": [dict(item) for item in self.repair_history],
            "bootstrap_attempted": self.bootstrap_attempted,
            "bootstrap_succeeded": self.bootstrap_succeeded,
            "bootstrap_attempt_count": self.bootstrap_attempt_count,
            "bootstrap_attempt_limit": self.bootstrap_attempt_limit,
            "bootstrap_history": [dict(item) for item in self.bootstrap_history],
            "cancel_requested": self.cancel_requested,
        }
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        default_skill_name: str = "",
        default_runtime: dict[str, Any] | None = None,
    ) -> "SkillRunRecord":
        data = dict(payload)
        command_plan_payload = (
            data.get("command_plan")
            if isinstance(data.get("command_plan"), dict)
            else data.get("shell_plan")
            if isinstance(data.get("shell_plan"), dict)
            else {}
        )
        skill_name = str(data.get("skill_name") or default_skill_name).strip()
        command_plan = SkillCommandPlan.from_dict(
            command_plan_payload,
            skill_name=skill_name,
            goal=str(data.get("goal", "")).strip(),
            user_query=str(data.get("user_query", "")).strip(),
            runtime_target=SkillRuntimeTarget.from_dict(data.get("runtime_target")),
            constraints=(
                data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
            ),
        )
        command = (
            str(data.get("command")).strip()
            if isinstance(data.get("command"), str) and data.get("command", "").strip()
            else command_plan.command
        )
        exit_code = data.get("exit_code")
        if not isinstance(exit_code, int):
            exit_code = None
        run_status = normalize_run_status(
            data.get("run_status", data.get("status")),
            success=bool(data.get("success")),
        )
        artifacts = data.get("artifacts")
        artifact_previews = data.get("artifact_previews")
        logs_preview = data.get("logs_preview")
        runtime = data.get("runtime")
        runtime_result = data.get("runtime_result")
        preflight = data.get("preflight")
        raw_repair_attempt_count = data.get("repair_attempt_count", 0)
        raw_repair_attempt_limit = data.get("repair_attempt_limit", 0)
        raw_bootstrap_attempt_count = data.get("bootstrap_attempt_count", 0)
        raw_bootstrap_attempt_limit = data.get("bootstrap_attempt_limit", 0)
        try:
            repair_attempt_count = max(0, int(raw_repair_attempt_count or 0))
        except (TypeError, ValueError):
            repair_attempt_count = 0
        try:
            repair_attempt_limit = max(0, int(raw_repair_attempt_limit or 0))
        except (TypeError, ValueError):
            repair_attempt_limit = 0
        try:
            bootstrap_attempt_count = max(0, int(raw_bootstrap_attempt_count or 0))
        except (TypeError, ValueError):
            bootstrap_attempt_count = 0
        try:
            bootstrap_attempt_limit = max(0, int(raw_bootstrap_attempt_limit or 0))
        except (TypeError, ValueError):
            bootstrap_attempt_limit = 0
        return cls(
            skill_name=skill_name,
            run_status=run_status,
            success=bool(data.get("success", run_status in {"planned", "running", "completed"})),
            summary=str(data.get("summary", "")).strip()
            or f"Skill run for '{skill_name}' is {run_status}.",
            command_plan=command_plan,
            run_id=(
                str(data.get("run_id")).strip()
                if isinstance(data.get("run_id"), str) and data.get("run_id", "").strip()
                else None
            ),
            command=command,
            workspace=(
                str(data.get("workspace")).strip()
                if isinstance(data.get("workspace"), str)
                and data.get("workspace", "").strip()
                else None
            ),
            started_at=(
                str(data.get("started_at")).strip()
                if isinstance(data.get("started_at"), str)
                and data.get("started_at", "").strip()
                else None
            ),
            finished_at=(
                str(data.get("finished_at")).strip()
                if isinstance(data.get("finished_at"), str)
                and data.get("finished_at", "").strip()
                else None
            ),
            exit_code=exit_code,
            failure_reason=(
                str(data.get("failure_reason")).strip()
                if isinstance(data.get("failure_reason"), str)
                and data.get("failure_reason", "").strip()
                else None
            ),
            artifacts=[
                dict(item)
                for item in (artifacts if isinstance(artifacts, list) else [])
                if isinstance(item, dict)
            ],
            artifact_previews=[
                dict(item)
                for item in (
                    artifact_previews if isinstance(artifact_previews, list) else []
                )
                if isinstance(item, dict)
            ],
            logs_preview=dict(logs_preview) if isinstance(logs_preview, dict) else {},
            runtime=dict(runtime) if isinstance(runtime, dict) else dict(default_runtime or {}),
            runtime_result=dict(runtime_result) if isinstance(runtime_result, dict) else {},
            execution_mode=(
                str(data.get("execution_mode")).strip()
                if isinstance(data.get("execution_mode"), str)
                and data.get("execution_mode", "").strip()
                else "shell"
            ),
            preflight=dict(preflight) if isinstance(preflight, dict) else {},
            repair_attempted=bool(data.get("repair_attempted", False)),
            repair_succeeded=bool(data.get("repair_succeeded", False)),
            repaired_from_run_id=(
                str(data.get("repaired_from_run_id")).strip()
                if isinstance(data.get("repaired_from_run_id"), str)
                and data.get("repaired_from_run_id", "").strip()
                else None
            ),
            repair_attempt_count=repair_attempt_count,
            repair_attempt_limit=repair_attempt_limit,
            repair_history=[
                dict(item)
                for item in (
                    data.get("repair_history") if isinstance(data.get("repair_history"), list) else []
                )
                if isinstance(item, dict)
            ],
            bootstrap_attempted=bool(data.get("bootstrap_attempted", False)),
            bootstrap_succeeded=bool(data.get("bootstrap_succeeded", False)),
            bootstrap_attempt_count=bootstrap_attempt_count,
            bootstrap_attempt_limit=bootstrap_attempt_limit,
            bootstrap_history=[
                dict(item)
                for item in (
                    data.get("bootstrap_history")
                    if isinstance(data.get("bootstrap_history"), list)
                    else []
                )
                if isinstance(item, dict)
            ],
            cancel_requested=bool(data.get("cancel_requested", False)),
        )
