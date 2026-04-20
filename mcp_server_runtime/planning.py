from __future__ import annotations

import ast
import json
import py_compile
import re
import shutil
from pathlib import Path
from typing import Any

from kg_agent.skills.command_planner import (
    _complete_json_via_text_first,
    SkillCommandPlanner,
    build_skill_doc_bundle,
    build_portable_script_command,
    default_generated_file_command,
    default_generated_file_entrypoint,
    extract_python_examples,
    is_dry_run,
    maybe_promote_inline_python_to_generated_script,
    normalize_free_shell_runtime_requirements,
    normalize_cli_args,
    normalize_generated_command,
    normalize_generated_entrypoint,
    normalize_generated_files,
    normalize_shell_command,
    normalize_shell_commands,
    summarize_cli_history,
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

GENERATED_FILE_REPAIR_PREVIEW_LIMIT = 2
GENERATED_FILE_REPAIR_PREVIEW_CHARS = 2400
KNOWN_NODE_ONLY_IMPORT_MODULES = {
    "pptxgenjs",
    "react",
    "react_dom",
    "react_icons",
    "sharp",
}


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
    doc_bundle = build_skill_doc_bundle(loaded_skill, request)
    cli_history = summarize_cli_history(repair_history)
    generated_file_previews = _compact_generated_file_previews(command_plan)
    system_prompt = (
        "You repair failed skill execution plans. "
        "When a conservative plan is stuck, you may upgrade it into a free-shell or generated-script plan. "
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
        f"{json.dumps({'skill_name': request.skill_name, 'goal': request.goal, 'user_query': request.user_query, 'constraints': request.constraints, 'effective_constraints': command_plan.constraints}, ensure_ascii=False, indent=2)}\n\n"
        "Previous command plan:\n"
        f"{json.dumps(command_plan.to_dict(), ensure_ascii=False, indent=2)}\n\n"
        "Generated file previews from the failed plan:\n"
        f"{json.dumps(generated_file_previews, ensure_ascii=False, indent=2)}\n\n"
        "Progressive skill document bundle:\n"
        f"{json.dumps(doc_bundle, ensure_ascii=False, indent=2)}\n\n"
        f"Current failure stage:\n{failure_stage}\n\n"
        f"Executed command:\n{command}\n\n"
        f"Exit code:\n{exit_code}\n\n"
        "Preflight result:\n"
        f"{json.dumps(preflight, ensure_ascii=False, indent=2)}\n\n"
        "Bounded CLI history:\n"
        f"{json.dumps(cli_history, ensure_ascii=False, indent=2)}\n\n"
        "stdout:\n"
        f"{stdout[-5000:]}\n\n"
        "stderr:\n"
        f"{stderr[-5000:]}\n\n"
        "Rules:\n"
        "1. Prefer the minimal repair that fixes the observed failure.\n"
        "2. Preserve the declared runtime target.\n"
        "3. Do not repeat the same failing command or generated entrypoint unless the new evidence clearly shows the prior failure was transient.\n"
        "4. Keep generated file paths relative and do not use heredocs.\n"
        "5. When you return generated_files, you may omit command and instead set entrypoint plus optional cli_args.\n"
        "6. When the task involves substantial Python logic, prefer generated_files plus entrypoint over a long python -c one-liner.\n"
        "7. If the failure is caused by missing dependencies or tools and setup can be done safely, return bootstrap_commands and explain them in bootstrap_reason.\n"
        "8. Treat natural-language dependency guidance anywhere in SKILL.md as valid repair input, even when there is no explicit Dependencies section.\n"
        "9. For isolated dependency bootstrap, prefer the provided bootstrap env variables instead of global installs. For Python packages, prefer commands such as python -m pip install --target \"$SKILL_BOOTSTRAP_SITE_PACKAGES\" <packages> on POSIX or python -m pip install --target $env:SKILL_BOOTSTRAP_SITE_PACKAGES <packages> on PowerShell. Plain python -m pip install <packages> is also acceptable because PIP_TARGET is preconfigured.\n"
        "10. For Node/global CLI bootstrap, prefer the provided bootstrap root via NPM_CONFIG_PREFIX instead of system-global npm installs.\n"
        "10a. Do not use pip to install Node-only packages. For example, use npm install -g pptxgenjs, not python -m pip install pptxgenjs.\n"
        "11. If the current failure is in preflight, fix the plan itself instead of restating the same invalid command.\n"
        "12. Treat the original goal, user query, effective constraints, the progressive document bundle, and the bounded CLI history as durable context. Use the CLI history to iteratively refine the next command instead of restarting from scratch.\n"
        "13. If the original request is vague but recoverable with low-risk defaults, fill them and continue instead of returning manual_required.\n"
        "14. If the failure cannot be repaired safely within the remaining repair budget, return manual_required.\n"
        "15. If the failed plan included generated_files, prefer the smallest edit that makes them runnable instead of regenerating an oversized helper.\n"
        "16. Do not import Node-only packages from Python. Use a Node entrypoint for Node/npm libraries.\n"
        "17. When the docs include concrete code examples or API names, follow those documented APIs exactly instead of inventing alternatives.\n"
        "Keep JSON valid and do not include markdown fences."
    )
    return system_prompt, user_prompt


def _compact_generated_file_previews(command_plan: SkillCommandPlan) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for generated_file in command_plan.generated_files[:GENERATED_FILE_REPAIR_PREVIEW_LIMIT]:
        previews.append(
            {
                "path": generated_file.path,
                "description": generated_file.description,
                "content_preview": str(generated_file.content)[:GENERATED_FILE_REPAIR_PREVIEW_CHARS],
            }
        )
    return previews


def _extract_python_import_roots(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = str(alias.name or "").split(".", 1)[0].strip()
                if root:
                    imports.append(root)
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "").split(".", 1)[0].strip()
            if module:
                imports.append(module)
    return imports


def _detect_node_only_python_imports(source: str) -> list[str]:
    imports = _extract_python_import_roots(source)
    return sorted(
        {
            item
            for item in imports
            if item.lower() in KNOWN_NODE_ONLY_IMPORT_MODULES
        }
    )


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


def _preflight_bootstrap_can_provision_tools(
    command_plan: SkillCommandPlan,
) -> bool:
    return (
        command_plan.shell_mode == "free_shell"
        and isinstance(command_plan.bootstrap_commands, list)
        and any(str(command or "").strip() for command in command_plan.bootstrap_commands)
    )


def _missing_tool_is_bootstrap_pending(
    *,
    tool_name: str,
    command_plan: SkillCommandPlan,
) -> bool:
    if not _preflight_bootstrap_can_provision_tools(command_plan):
        return False
    normalized_tool = str(tool_name or "").strip().lower()
    if not normalized_tool or normalized_tool in {"python", "pip"}:
        return False
    if normalized_tool in {"node", "npm", "npx", "yarn", "pnpm"}:
        return True
    bootstrap_commands = [
        str(command or "").strip().lower()
        for command in command_plan.bootstrap_commands
        if str(command or "").strip()
    ]
    return any(normalized_tool in command for command in bootstrap_commands)


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
        missing_tools: list[str] = []
        for item in required_tools:
            available = bool(item.get("available"))
            bootstrap_pending = bool(
                not available
                and _missing_tool_is_bootstrap_pending(
                    tool_name=str(item.get("name", "")).strip(),
                    command_plan=command_plan,
                )
            )
            item["bootstrap_pending"] = bootstrap_pending
            if not available and not bootstrap_pending:
                missing_tools.append(str(item.get("name", "")).strip())
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
            node_only_imports = _detect_node_only_python_imports(
                target.read_text(encoding="utf-8")
            )
            item["python_imports"] = node_only_imports
            if node_only_imports:
                failure_reason = "generated_python_node_only_import"
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
    try:
        payload = await llm_client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=3200,
        )
    except Exception:
        payload = await _complete_json_via_text_first(
            llm_client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=3200,
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
    (
        bootstrap_commands,
        required_tools,
        warnings,
        bootstrap_reason,
    ) = normalize_free_shell_runtime_requirements(
        runtime_target=command_plan.runtime_target,
        command=base_plan.command,
        entrypoint=base_plan.entrypoint,
        generated_files=list(base_plan.generated_files),
        bootstrap_commands=list(base_plan.bootstrap_commands),
        required_tools=required_tools,
        warnings=warnings,
        bootstrap_reason=base_plan.bootstrap_reason,
    )
    repaired_plan = SkillCommandPlan(
        skill_name=base_plan.skill_name,
        goal=base_plan.goal,
        user_query=base_plan.user_query,
        runtime_target=base_plan.runtime_target,
        constraints=dict(base_plan.constraints),
        command=base_plan.command,
        mode=base_plan.mode,
        shell_mode="free_shell",
        rationale=base_plan.rationale,
        entrypoint=base_plan.entrypoint,
        cli_args=list(base_plan.cli_args),
        generated_files=list(base_plan.generated_files),
        bootstrap_commands=list(bootstrap_commands),
        bootstrap_reason=bootstrap_reason,
        missing_fields=list(base_plan.missing_fields),
        failure_reason=base_plan.failure_reason,
        hints={
            **dict(command_plan.hints),
            "planner": "free_shell",
            "shell_mode_requested": dict(command_plan.hints).get(
                "shell_mode_requested",
                command_plan.shell_mode,
            ),
            "shell_mode_effective": "free_shell",
            "shell_mode_escalated": (
                str(
                    dict(command_plan.hints).get(
                        "shell_mode_requested",
                        command_plan.shell_mode,
                    )
                ).strip().lower()
                != "free_shell"
            ),
            "shell_mode_escalation_reason": (
                str(base_plan.failure_reason or "").strip()
                or f"runtime_{failure_stage}_repair"
            ),
            "planning_blockers": [
                {
                    "failure_reason": str(base_plan.failure_reason or "").strip() or None,
                    "missing_fields": list(base_plan.missing_fields),
                    "rationale": str(base_plan.rationale or "").strip() or None,
                }
            ],
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
                "path": payload["path"],
            },
        }
    )
    return data
