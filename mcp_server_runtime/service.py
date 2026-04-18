from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from kg_agent.skills.command_planner import is_dry_run
from kg_agent.skills.models import LoadedSkill, SkillCommandPlan, SkillExecutionRequest, SkillRunRecord

from .errors import SkillServerError


@dataclass
class RuntimeServiceDeps:
    build_skill_catalog_entry: Callable[[Path], dict[str, Any]]
    build_skill_runtime_context: Callable[
        [str, str, str, str | None, dict[str, Any]],
        tuple[dict[str, Any], LoadedSkill, SkillExecutionRequest],
    ]
    build_command: Callable[[Path, list[str]], list[str]]
    build_loaded_skill: Callable[[str], LoadedSkill]
    build_loaded_skill_from_dir: Callable[[Path], LoadedSkill]
    build_run_record_payload: Callable[..., dict[str, Any]]
    build_script_env: Callable[..., dict[str, str]]
    classify_skill_file: Callable[[Path, Path], str]
    default_run_timeout_s: int
    default_runtime_target: Any
    default_script_timeout_s: int
    get_llm_client: Callable[[], Any]
    load_run_row: Callable[[str], Any]
    load_skill_payload: Callable[[str], dict[str, Any]]
    materialize_shell_command: Callable[..., str | None]
    mark_run_cancel_requested: Callable[[str], None]
    max_reference_bytes: int
    now: Callable[[], str]
    prefetch_skill_wheels_for_skill: Callable[[LoadedSkill], dict[str, Any]]
    prepare_transport_payload: Callable[[dict[str, Any]], dict[str, Any]]
    read_text: Callable[[Path], str]
    recover_stale_running_record: Callable[[str], dict[str, Any] | None]
    request_with_runtime_target: Callable[[SkillExecutionRequest, Any], SkillExecutionRequest]
    resolve_command_plan: Callable[..., Awaitable[SkillCommandPlan]]
    resolve_script_path: Callable[[Path, str], Path]
    resolve_skill_dir: Callable[[str], Path]
    resolve_skill_file_path: Callable[[Path, str], Path]
    resolve_wait_for_completion: Callable[..., bool]
    run_shell_task: Callable[..., Awaitable[dict[str, Any]]]
    runs_root: Path
    score_skill_match: Callable[[str], Any]
    skill_dirs: Callable[[], list[Path]]
    skill_run_record_cls: type[SkillRunRecord]
    store_run_record: Callable[[dict[str, Any]], None]
    terminate_pid: Callable[[int | None], None]
    wait_for_terminal_run_record: Callable[..., Awaitable[dict[str, Any]]]
    write_json_atomic: Callable[[Path, dict[str, Any]], None]
    cancel_wait_timeout_s: float
    call_tool_result_cls: type[Any]
    text_content_cls: type[Any]


class RuntimeService:
    def __init__(self, deps: RuntimeServiceDeps):
        self.deps = deps

    def list_skills(self, query: str | None = None) -> dict[str, Any]:
        skills = [self.deps.build_skill_catalog_entry(skill_dir) for skill_dir in self.deps.skill_dirs()]
        selected_skill = None
        if query:
            ranked = sorted(
                (
                    (
                        self.deps.score_skill_match(
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

    def read_skill(self, skill_name: str) -> dict[str, Any]:
        payload = self.deps.load_skill_payload(skill_name)
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

    def read_skill_file(self, skill_name: str, relative_path: str) -> dict[str, Any]:
        skill_dir = self.deps.resolve_skill_dir(skill_name)
        target = self.deps.resolve_skill_file_path(skill_dir, relative_path)
        content = self.deps.read_text(target)
        byte_length = len(content.encode("utf-8"))
        truncated = False
        if byte_length > self.deps.max_reference_bytes:
            content = content.encode("utf-8")[: self.deps.max_reference_bytes].decode(
                "utf-8",
                errors="ignore",
            )
            truncated = True
        return {
            "summary": f"Read skill file '{relative_path}' from '{skill_name}'",
            "skill_name": skill_name,
            "path": str(target.relative_to(skill_dir)).replace("\\", "/"),
            "kind": self.deps.classify_skill_file(skill_dir, target),
            "content": content,
            "truncated": truncated,
        }

    def read_skill_docs(self, skill_name: str) -> dict[str, Any]:
        payload = self.read_skill(skill_name)
        payload["summary"] = f"Loaded docs for skill '{skill_name}'"
        return payload

    async def run_skill_task(
        self,
        *,
        skill_name: str,
        goal: str,
        user_query: str | None = None,
        workspace: str | None = None,
        constraints: dict[str, Any] | None = None,
        command_plan: dict[str, Any] | None = None,
        timeout_s: int | None = None,
        cleanup_workspace: bool = False,
        wait_for_completion: bool | None = None,
    ) -> dict[str, Any]:
        normalized_constraints = constraints or {}
        payload, loaded_skill, request = self.deps.build_skill_runtime_context(
            skill_name=skill_name,
            goal=goal,
            user_query=user_query or "",
            workspace=workspace,
            constraints=normalized_constraints,
        )
        resolved_command_plan = await self.deps.resolve_command_plan(
            loaded_skill=loaded_skill,
            request=request,
            command_plan_payload=command_plan,
            llm_client=self.deps.get_llm_client(),
        )
        request = self.deps.request_with_runtime_target(
            request,
            resolved_command_plan.runtime_target,
        )
        resolved_wait_for_completion = self.deps.resolve_wait_for_completion(
            constraints=normalized_constraints,
            wait_for_completion=wait_for_completion,
        )

        if resolved_command_plan.is_manual_required:
            now = self.deps.now()
            run_record = self.deps.skill_run_record_cls(
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
            response = self.deps.build_run_record_payload(
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
            self.deps.store_run_record(response)
            return self.deps.prepare_transport_payload(response)

        materialized_command = self.deps.materialize_shell_command(
            loaded_skill=loaded_skill,
            command_plan=resolved_command_plan,
        )
        if is_dry_run(normalized_constraints):
            now = self.deps.now()
            run_record = self.deps.skill_run_record_cls(
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
            response = self.deps.build_run_record_payload(
                record=run_record,
                goal=goal,
                user_query=user_query or "",
                constraints=normalized_constraints,
                payload=payload,
                loaded_skill=loaded_skill,
            )
            response["notes"] = "Dry run only; the shell command was planned but not executed."
            response["logs"] = {"stdout": "", "stderr": ""}
            self.deps.store_run_record(response)
            return self.deps.prepare_transport_payload(response)

        return await self.deps.run_shell_task(
            skill_payload=payload,
            loaded_skill=loaded_skill,
            request=request,
            command_plan=resolved_command_plan,
            timeout_s=self.deps.default_run_timeout_s if timeout_s is None else int(timeout_s),
            cleanup_workspace=cleanup_workspace,
            wait_for_completion=resolved_wait_for_completion,
        )

    def _require_record(self, run_id: str) -> dict[str, Any]:
        record = self.deps.recover_stale_running_record(run_id)
        if record is None:
            raise SkillServerError(f"Unknown run_id: {run_id}")
        return record

    def get_run_status(self, run_id: str) -> dict[str, Any]:
        record = self._require_record(run_id)
        return self.deps.prepare_transport_payload(
            {
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
                "repair_attempt_count": int(record.get("repair_attempt_count", 0) or 0),
                "repair_attempt_limit": int(record.get("repair_attempt_limit", 0) or 0),
                "repair_history": record.get("repair_history", []),
                "bootstrap_attempted": bool(record.get("bootstrap_attempted", False)),
                "bootstrap_succeeded": bool(record.get("bootstrap_succeeded", False)),
                "bootstrap_attempt_count": int(record.get("bootstrap_attempt_count", 0) or 0),
                "bootstrap_attempt_limit": int(record.get("bootstrap_attempt_limit", 0) or 0),
                "bootstrap_history": record.get("bootstrap_history", []),
                "cancel_requested": bool(record.get("cancel_requested", False)),
            }
        )

    async def cancel_skill_run(
        self,
        run_id: str,
        *,
        wait_for_termination: bool = True,
    ) -> dict[str, Any]:
        record = self._require_record(run_id)
        if str(record.get("run_status", "")).strip() != "running":
            return self.get_run_status(run_id)
        self.deps.mark_run_cancel_requested(run_id)
        row = self.deps.load_run_row(run_id)
        if row is not None and row["active_process_pid"] is not None:
            self.deps.terminate_pid(int(row["active_process_pid"]))

        if wait_for_termination:
            terminal = await self.deps.wait_for_terminal_run_record(
                run_id=run_id,
                timeout_s=max(0.5, self.deps.cancel_wait_timeout_s),
            )
            if str(terminal.get("run_status", "")).strip() != "running":
                return self.get_run_status(run_id)
        return self.get_run_status(run_id)

    def get_run_logs(self, run_id: str) -> dict[str, Any]:
        record = self._require_record(run_id)
        logs = record.get("logs", {})
        return self.deps.prepare_transport_payload(
            {
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
                "repair_attempt_count": int(record.get("repair_attempt_count", 0) or 0),
                "repair_attempt_limit": int(record.get("repair_attempt_limit", 0) or 0),
                "repair_history": record.get("repair_history", []),
                "bootstrap_attempted": bool(record.get("bootstrap_attempted", False)),
                "bootstrap_succeeded": bool(record.get("bootstrap_succeeded", False)),
                "bootstrap_attempt_count": int(record.get("bootstrap_attempt_count", 0) or 0),
                "bootstrap_attempt_limit": int(record.get("bootstrap_attempt_limit", 0) or 0),
                "bootstrap_history": record.get("bootstrap_history", []),
                "cancel_requested": bool(record.get("cancel_requested", False)),
                "stdout": logs.get("stdout", ""),
                "stderr": logs.get("stderr", ""),
            }
        )

    def get_run_artifacts(self, run_id: str) -> dict[str, Any]:
        record = self._require_record(run_id)
        return self.deps.prepare_transport_payload(
            {
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
                "repair_attempt_count": int(record.get("repair_attempt_count", 0) or 0),
                "repair_attempt_limit": int(record.get("repair_attempt_limit", 0) or 0),
                "repair_history": record.get("repair_history", []),
                "bootstrap_attempted": bool(record.get("bootstrap_attempted", False)),
                "bootstrap_succeeded": bool(record.get("bootstrap_succeeded", False)),
                "bootstrap_attempt_count": int(record.get("bootstrap_attempt_count", 0) or 0),
                "bootstrap_attempt_limit": int(record.get("bootstrap_attempt_limit", 0) or 0),
                "bootstrap_history": record.get("bootstrap_history", []),
                "cancel_requested": bool(record.get("cancel_requested", False)),
                "artifacts": record.get("artifacts", []),
            }
        )

    async def execute_skill_script(
        self,
        *,
        skill_name: str,
        script_name: str,
        args: list[str] | None = None,
        timeout_s: int | None = None,
        cleanup_workspace: bool = False,
    ) -> Any:
        skill_dir = self.deps.resolve_skill_dir(skill_name)
        script_path = self.deps.resolve_script_path(skill_dir, script_name)
        relative_script = str(script_path.relative_to(skill_dir)).replace("\\", "/")
        self.deps.runs_root.mkdir(parents=True, exist_ok=True)
        workspace_dir = Path(
            tempfile.mkdtemp(prefix=f"{skill_dir.name}-", dir=str(self.deps.runs_root))
        ).resolve()
        command = self.deps.build_command(script_path, [str(item) for item in (args or [])])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace_dir),
                env=self.deps.build_script_env(
                    skill_dir,
                    workspace_dir,
                    relative_script=relative_script,
                    default_runtime_target=self.deps.default_runtime_target,
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=max(
                        1,
                        int(
                            self.deps.default_script_timeout_s
                            if timeout_s is None
                            else timeout_s
                        ),
                    ),
                )
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.wait()
                raise SkillServerError(
                    f"Script timed out after "
                    f"{self.deps.default_script_timeout_s if timeout_s is None else timeout_s} "
                    f"seconds: {script_name}"
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
            return self.deps.call_tool_result_cls(
                content=[self.deps.text_content_cls(type="text", text=text)],
                structuredContent=result_payload,
                isError=process.returncode != 0,
            )
        finally:
            if cleanup_workspace:
                shutil.rmtree(workspace_dir, ignore_errors=True)

    def read_skill_reference(self, skill_name: str, reference_name: str) -> str:
        skill_dir = self.deps.resolve_skill_dir(skill_name)
        references_dir = (skill_dir / "references").resolve()
        if not references_dir.is_dir():
            raise SkillServerError(f"Skill has no references directory: {skill_name}")
        ref_path = (references_dir / reference_name).resolve()
        if ref_path == references_dir or references_dir not in ref_path.parents:
            raise SkillServerError("reference_name must resolve inside references/")
        if not ref_path.is_file():
            raise SkillServerError(f"Unknown reference: {reference_name}")
        return self.deps.read_text(ref_path)

    def prefetch_skill_wheels_cli(self, *, skill_name: str | None = None) -> dict[str, Any]:
        if skill_name and skill_name.strip():
            targets = [self.deps.build_loaded_skill(skill_name.strip())]
        else:
            targets = [
                self.deps.build_loaded_skill_from_dir(path)
                for path in self.deps.skill_dirs()
            ]
        results = [self.deps.prefetch_skill_wheels_for_skill(skill) for skill in targets]
        return {
            "summary": (
                f"Prefetched wheels for {len(results)} skill(s)."
                if results
                else "No skills found."
            ),
            "results": results,
        }
