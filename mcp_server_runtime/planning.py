from __future__ import annotations

import json
import py_compile
import shutil
from pathlib import Path
from typing import Any

from kg_agent.skills.command_planner import (
    SkillCommandPlanner,
    build_portable_script_command,
    build_shell_hints as build_skill_shell_hints,
    default_generated_file_command,
    default_generated_file_entrypoint,
    extract_python_examples,
    is_dry_run,
    maybe_promote_inline_python_to_generated_script,
    normalize_cli_args,
    normalize_generated_command,
    normalize_generated_entrypoint,
    normalize_generated_files,
    normalize_shell_command,
    normalize_shell_commands,
)
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillRunRecord,
)

from .config import DEFAULT_RUNTIME_TARGET, MAX_GENERATED_FILE_BYTES, SKILL_RUNTIME_CONFIG
from .envs import (
    _build_script_shell_command,
    _build_workspace_script_shell_command,
    _rewrite_command_for_workspace,
)
from .skills import _build_loaded_skill, _load_skill_payload
from .errors import SkillServerError


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


def _request_with_runtime_target(
    request: SkillExecutionRequest,
    runtime_target,
) -> SkillExecutionRequest:
    return SkillExecutionRequest(
        skill_name=request.skill_name,
        goal=request.goal,
        user_query=request.user_query,
        workspace=request.workspace,
        shell_mode=request.shell_mode,
        runtime_target=runtime_target,
        constraints=dict(request.constraints),
    )


async def _resolve_command_plan(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    command_plan_payload: dict[str, Any] | None,
    llm_client: Any,
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
        llm_client=llm_client,
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
    failure_stage: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    preflight: dict[str, Any],
    repair_history: list[dict[str, Any]],
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
        '"entrypoint": str | null, '
        '"cli_args": [str], '
        '"generated_files": [{"path": str, "content": str, "description": str}], '
        '"bootstrap_commands": [str], '
        '"bootstrap_reason": str | null, '
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
        f"Current failure stage:\n{failure_stage}\n\n"
        f"Executed command:\n{command}\n\n"
        f"Exit code:\n{exit_code}\n\n"
        "Preflight result:\n"
        f"{json.dumps(preflight, ensure_ascii=False, indent=2)}\n\n"
        "Prior repair history:\n"
        f"{json.dumps(repair_history[-4:], ensure_ascii=False, indent=2)}\n\n"
        "stdout:\n"
        f"{stdout[-5000:]}\n\n"
        "stderr:\n"
        f"{stderr[-5000:]}\n\n"
        "Skill docs excerpt:\n"
        f"{loaded_skill.skill_md[:7000]}\n\n"
        "Rules:\n"
        "1. Prefer the minimal repair that fixes the observed failure.\n"
        "2. Preserve the declared runtime target.\n"
        "3. Do not repeat the same failing command or generated entrypoint unless the new evidence clearly shows the prior failure was transient.\n"
        "4. Keep generated file paths relative and do not use heredocs.\n"
        "5. When you return generated_files, you may omit command and instead set entrypoint plus optional cli_args.\n"
        "6. When the task involves substantial Python logic, prefer generated_files plus entrypoint over a long python -c one-liner.\n"
        "7. If the failure is caused by missing dependencies or tools and setup can be done safely, return bootstrap_commands and explain them in bootstrap_reason.\n"
        "8. For Python package bootstrap, prefer workspace-local commands such as python -m pip install --target ./.skill_bootstrap/site-packages <packages>.\n"
        "9. If the current failure is in preflight, fix the plan itself instead of restating the same invalid command.\n"
        "10. If the failure cannot be repaired safely within the remaining repair budget, return manual_required.\n"
        "Keep JSON valid and do not include markdown fences."
    )
    return system_prompt, user_prompt


def _preflight_required_tools(
    command_plan: SkillCommandPlan,
    *,
    search_path: str | None = None,
) -> list[dict[str, Any]]:
    required_tools = command_plan.hints.get("required_tools", [])
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_tool in required_tools if isinstance(required_tools, list) else []:
        tool_name = str(raw_tool).strip()
        if not tool_name or tool_name in seen:
            continue
        seen.add(tool_name)
        resolved = shutil.which(tool_name, path=search_path)
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
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    generated_file_specs = _validate_generated_file_specs(command_plan)
    required_tools = _preflight_required_tools(
        command_plan,
        search_path=(env or {}).get("PATH"),
    )
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
    failure_stage: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    preflight: dict[str, Any],
    repair_history: list[dict[str, Any]],
    llm_client: Any,
) -> SkillCommandPlan | None:
    if not llm_client.is_available():
        return None
    system_prompt, user_prompt = _build_free_shell_repair_prompt(
        loaded_skill=loaded_skill,
        request=request,
        command_plan=command_plan,
        command=command,
        failure_stage=failure_stage,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        preflight=preflight,
        repair_history=repair_history,
    )
    payload = await llm_client.complete_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.0,
        max_tokens=1800,
    )
    generated_files = normalize_generated_files(payload.get("generated_files"))
    cli_args = normalize_cli_args(payload.get("cli_args")) or []
    bootstrap_commands = normalize_shell_commands(payload.get("bootstrap_commands"))
    bootstrap_reason = str(payload.get("bootstrap_reason", "")).strip()
    entrypoint = normalize_generated_entrypoint(
        payload.get("entrypoint"),
        generated_files=generated_files,
    )
    command_payload = normalize_shell_command(payload.get("command"))
    command_payload = normalize_generated_command(command_payload, generated_files)
    if command_payload is None and entrypoint:
        command_payload = build_portable_script_command(
            entrypoint,
            cli_args,
            runtime_target=command_plan.runtime_target,
        )
    if command_payload is None and generated_files:
        command_payload = default_generated_file_command(
            generated_files,
            runtime_target=command_plan.runtime_target,
            cli_args=cli_args,
        )
        if command_payload:
            entrypoint = entrypoint or default_generated_file_entrypoint(generated_files)
    (
        command_payload,
        generated_files,
        entrypoint,
        cli_args,
        promoted_inline_python,
    ) = maybe_promote_inline_python_to_generated_script(
        command=command_payload,
        generated_files=generated_files,
        entrypoint=entrypoint,
        cli_args=cli_args,
        runtime_target=command_plan.runtime_target,
        request_text="\n".join([request.goal, request.user_query]),
        python_example_count=len(extract_python_examples(loaded_skill.skill_md)),
    )
    normalized_payload = dict(payload)
    normalized_payload["command"] = command_payload
    normalized_payload["generated_files"] = [item.to_dict() for item in generated_files]
    normalized_payload["entrypoint"] = entrypoint
    normalized_payload["cli_args"] = list(cli_args)
    normalized_payload["bootstrap_commands"] = list(bootstrap_commands)
    normalized_payload["bootstrap_reason"] = bootstrap_reason
    if generated_files and str(normalized_payload.get("mode", "")).strip().lower() != "manual_required":
        normalized_payload["mode"] = "generated_script"
    base_plan = SkillCommandPlan.from_dict(
        normalized_payload,
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
        bootstrap_commands=list(base_plan.bootstrap_commands),
        bootstrap_reason=base_plan.bootstrap_reason,
        missing_fields=list(base_plan.missing_fields),
        failure_reason=base_plan.failure_reason,
        hints={
            **dict(command_plan.hints),
            "planner": "free_shell",
            "required_tools": required_tools,
            "warnings": (
                [
                    *warnings,
                    "Promoted a repair inline Python command into a generated script bundle.",
                ]
                if promoted_inline_python
                else warnings
            ),
            "promoted_inline_python_to_generated_script": promoted_inline_python,
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
        return _rewrite_command_for_workspace(
            command=command_plan.command,
            loaded_skill=loaded_skill,
            workspace_dir=workspace_dir,
            runtime_target=command_plan.runtime_target,
        )
    if command_plan.mode == "generated_script":
        relative_script = command_plan.entrypoint
        if not relative_script and command_plan.generated_files:
            relative_script = command_plan.generated_files[0].path
        if workspace_dir is not None:
            if relative_script:
                return _build_workspace_script_shell_command(
                    workspace_dir=workspace_dir,
                    relative_script=relative_script,
                    cli_args=command_plan.cli_args,
                    runtime_target=command_plan.runtime_target,
                )
            return command_plan.command
        if relative_script:
            return build_portable_script_command(
                relative_script,
                command_plan.cli_args,
                runtime_target=command_plan.runtime_target,
            )
        return _rewrite_command_for_workspace(
            command=command_plan.command,
            loaded_skill=loaded_skill,
            workspace_dir=workspace_dir,
            runtime_target=command_plan.runtime_target,
        )
    if command_plan.entrypoint:
        if workspace_dir is not None:
            return _build_workspace_script_shell_command(
                workspace_dir=workspace_dir,
                relative_script=command_plan.entrypoint,
                cli_args=command_plan.cli_args,
                runtime_target=command_plan.runtime_target,
            )
        return _build_script_shell_command(
            skill_dir=loaded_skill.skill.path,
            relative_script=command_plan.entrypoint,
            cli_args=command_plan.cli_args,
        )
    return _rewrite_command_for_workspace(
        command=command_plan.command,
        loaded_skill=loaded_skill,
        workspace_dir=workspace_dir,
        runtime_target=command_plan.runtime_target,
    )


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
