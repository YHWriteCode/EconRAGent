from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from kg_agent.agent.prompts import build_skill_free_shell_planner_prompt
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillGeneratedFile,
    SkillRuntimeTarget,
    SkillShellMode,
    normalize_shell_mode,
)


CODE_FENCE_PATTERN = re.compile(
    r"```(?P<language>[A-Za-z0-9_+-]*)\s*\n(?P<code>.*?)```",
    re.IGNORECASE | re.DOTALL,
)
CREDENTIAL_KEYWORD_PATTERN = re.compile(
    r"(api[_ -]?key|access[_ -]?token|credential|secret|password|auth token)",
    re.IGNORECASE,
)
ENV_VAR_PATTERN = re.compile(
    r"\$(?:\{(?P<braced>[A-Z][A-Z0-9_]+)\}|(?P<unix>[A-Z][A-Z0-9_]+))|%(?P<win>[A-Z][A-Z0-9_]+)%",
)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
MAX_SCRIPT_PREVIEW_CHARS = 1400
MAX_MARKDOWN_EXCERPT_CHARS = 7000
MAX_GENERATED_FILE_COUNT = 5
MAX_GENERATED_FILE_BYTES = 64000


class SkillPlannerLLMClient(Protocol):
    def is_available(self) -> bool:
        ...

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        ...


def split_skill_markdown(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    return {}, raw[end + len("\n---\n") :]


def _tokenize(text: str) -> set[str]:
    return {match.group(0).casefold() for match in TOKEN_PATTERN.finditer(text or "")}


def _score_relevance(text: str, query: str) -> int:
    query_tokens = _tokenize(query)
    if not query_tokens:
        return 0
    return len(query_tokens & _tokenize(text))


def _truncate_text(text: str, *, max_chars: int) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "\n...[truncated]"


def extract_code_examples(
    markdown: str,
    *,
    languages: set[str] | None = None,
) -> list[dict[str, str]]:
    _, body = split_skill_markdown(markdown)
    examples: list[dict[str, str]] = []
    for match in CODE_FENCE_PATTERN.finditer(body):
        language = str(match.group("language") or "").strip().lower()
        if languages is not None and language not in languages:
            continue
        code = str(match.group("code") or "").strip()
        if not code:
            continue
        examples.append({"language": language or "text", "code": code})
    return examples


def select_relevant_examples(
    examples: list[dict[str, str]],
    *,
    query_text: str,
    limit: int = 4,
    max_chars: int = 1800,
) -> list[dict[str, str]]:
    ranked = sorted(
        examples,
        key=lambda item: (
            _score_relevance(item.get("code", ""), query_text),
            len(item.get("code", "")),
        ),
        reverse=True,
    )
    selected: list[dict[str, str]] = []
    for item in ranked[: max(1, limit)]:
        selected.append(
            {
                "language": str(item.get("language", "")).strip() or "text",
                "code": _truncate_text(str(item.get("code", "")), max_chars=max_chars),
            }
        )
    return selected


def extract_shell_examples(markdown: str) -> list[str]:
    _, body = split_skill_markdown(markdown)
    examples: list[str] = []
    for item in extract_code_examples(
        markdown,
        languages={"bash", "sh", "shell", "powershell", "ps1"},
    ):
        for line in item["code"].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            examples.append(stripped)

    if not examples:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(
                (
                    "python ",
                    "/bin/sh ",
                    "bash ",
                    "powershell ",
                    "./scripts/",
                    "scripts/",
                )
            ):
                examples.append(stripped)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for command in examples:
        if command in seen:
            continue
        seen.add(command)
        deduplicated.append(command)
    return deduplicated


def extract_python_examples(markdown: str) -> list[dict[str, str]]:
    return extract_code_examples(markdown, languages={"python", "py"})


def iter_runnable_scripts(loaded_skill: LoadedSkill) -> list[str]:
    runnable: list[str] = []
    for item in loaded_skill.file_inventory:
        if item.kind != "script":
            continue
        relative_path = item.path.replace("\\", "/")
        suffix = Path(relative_path).suffix.lower()
        absolute_path = (loaded_skill.skill.path / relative_path).resolve()
        if suffix in {".py", ".sh", ".bash", ".ps1"} or os.access(absolute_path, os.X_OK):
            runnable.append(relative_path)
    return runnable


def build_portable_script_command(
    entrypoint: str,
    cli_args: list[str] | None = None,
    *,
    runtime_target: SkillRuntimeTarget | None = None,
) -> str:
    _ = runtime_target or SkillRuntimeTarget.linux_default()
    normalized_entrypoint = entrypoint.replace("\\", "/")
    if normalized_entrypoint.startswith(".") and not normalized_entrypoint.startswith(("./", "../")):
        normalized_entrypoint = f"./{normalized_entrypoint}"
    suffix = Path(normalized_entrypoint).suffix.lower()
    argv: list[str]
    if suffix == ".py":
        argv = ["python", normalized_entrypoint]
    elif suffix in {".sh", ".bash"}:
        argv = ["bash" if suffix == ".bash" else "/bin/sh", normalized_entrypoint]
    elif suffix == ".ps1":
        argv = ["powershell", "-File", normalized_entrypoint]
    else:
        argv = [normalized_entrypoint]
    argv.extend(str(item) for item in (cli_args or []))
    return shlex.join(argv)


def default_generated_file_command(
    generated_files: list[SkillGeneratedFile],
    *,
    runtime_target: SkillRuntimeTarget | None = None,
) -> str | None:
    if len(generated_files) != 1:
        return None
    return build_portable_script_command(
        generated_files[0].path,
        runtime_target=runtime_target,
    )


def normalize_generated_command(
    command: str | None,
    generated_files: list[SkillGeneratedFile],
) -> str | None:
    if not command:
        return command
    normalized = command
    for generated_file in generated_files:
        path = generated_file.path.replace("\\", "/").strip()
        if not path or not path.startswith(".") or path.startswith(("./", "../")):
            continue
        if f"./{path}" in normalized:
            continue
        normalized = normalized.replace(path, f"./{path}")
    return normalized


def build_shell_hints(loaded_skill: LoadedSkill) -> dict[str, Any]:
    example_commands = extract_shell_examples(loaded_skill.skill_md)
    runnable_scripts = iter_runnable_scripts(loaded_skill)
    auto_generated_commands = [
        build_portable_script_command(relative_path) for relative_path in runnable_scripts[:3]
    ]
    required_credentials = extract_required_credentials(
        markdown=loaded_skill.skill_md,
        example_commands=example_commands,
    )
    return {
        "execution_mode": "shell",
        "example_commands": example_commands[:5],
        "auto_generated_commands": auto_generated_commands,
        "runnable_scripts": runnable_scripts,
        "python_example_count": len(extract_python_examples(loaded_skill.skill_md)),
        "required_credentials": required_credentials,
        "notes": (
            "Provide constraints.shell_command / constraints.command for an explicit shell task, "
            "or provide structured CLI args in constraints.args / constraints.cli_args when the skill "
            "has a single runnable entrypoint. Set constraints.shell_mode='free_shell' to enable "
            "LLM-driven shell planning and optional generated scripts."
        ),
    }


def normalize_shell_command(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        argv = [str(item) for item in value if isinstance(item, (str, int, float))]
        if argv:
            return shlex.join(argv)
    return None


def extract_requested_shell_command(constraints: dict[str, Any]) -> str | None:
    for key in ("shell_command", "command"):
        command = normalize_shell_command(constraints.get(key))
        if command:
            return command
    return None


def is_dry_run(constraints: dict[str, Any]) -> bool:
    return bool(constraints.get("dry_run") or constraints.get("plan_only"))


def normalize_cli_args(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    if not isinstance(value, dict):
        return None

    normalized: list[str] = []
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key:
            continue
        flag = key if key.startswith("-") else (f"-{key}" if len(key) == 1 else f"--{key}")
        if isinstance(raw_value, bool):
            if raw_value:
                normalized.append(flag)
            continue
        if raw_value is None:
            continue
        if isinstance(raw_value, (list, tuple)):
            for item in raw_value:
                normalized.extend([flag, str(item)])
            continue
        normalized.extend([flag, str(raw_value)])
    return normalized


def extract_structured_cli_args(constraints: dict[str, Any]) -> list[str] | None:
    for key in ("cli_args", "args", "command_args"):
        normalized = normalize_cli_args(constraints.get(key))
        if normalized is not None:
            return normalized
    return None


def extract_required_credentials(
    *,
    markdown: str,
    example_commands: list[str] | None = None,
) -> list[str]:
    required: list[str] = []
    seen: set[str] = set()
    corpus = "\n".join([markdown, *(example_commands or [])])
    for match in ENV_VAR_PATTERN.finditer(corpus):
        candidate = (
            match.group("braced") or match.group("unix") or match.group("win") or ""
        ).strip()
        normalized = candidate.upper()
        if not normalized or normalized in seen:
            continue
        if not any(token in normalized for token in ("KEY", "TOKEN", "SECRET", "PASS")):
            continue
        required.append(normalized)
        seen.add(normalized)
    if required:
        return required
    if CREDENTIAL_KEYWORD_PATTERN.search(markdown):
        return ["CREDENTIAL"]
    return []


def has_credential_values(constraints: dict[str, Any]) -> bool:
    credential_keys = ("api_key", "token", "credential", "secret", "password")
    for key, value in constraints.items():
        key_lower = str(key).strip().lower()
        if any(token in key_lower for token in credential_keys):
            if isinstance(value, str) and value.strip():
                return True
            if value not in (None, "", False):
                return True
    env_payload = constraints.get("env")
    if isinstance(env_payload, dict):
        for key, value in env_payload.items():
            key_lower = str(key).strip().lower()
            if any(token in key_lower for token in credential_keys):
                if isinstance(value, str) and value.strip():
                    return True
                if value not in (None, "", False):
                    return True
    return False


def extract_required_cli_flags(script_path: Path) -> list[str]:
    try:
        raw = script_path.read_text(encoding="utf-8")
    except Exception:
        return []
    pattern = re.compile(
        r"add_argument\(\s*['\"](?P<flag>--[a-zA-Z0-9_-]+)['\"][^)]*required\s*=\s*True",
        re.DOTALL,
    )
    return [match.group("flag") for match in pattern.finditer(raw)]


def extract_input_paths(constraints: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    single = constraints.get("input_path")
    if isinstance(single, str) and single.strip():
        candidate = single.strip()
        paths.append(candidate)
        seen.add(candidate)
    multi = constraints.get("input_paths")
    if isinstance(multi, list):
        for item in multi:
            if not isinstance(item, str) or not item.strip():
                continue
            candidate = item.strip()
            if candidate in seen:
                continue
            paths.append(candidate)
            seen.add(candidate)
    for key in ("file_path", "path"):
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if candidate not in seen:
                paths.append(candidate)
                seen.add(candidate)
    return paths


def extract_output_path(constraints: dict[str, Any]) -> str | None:
    for key in ("output_path", "output", "destination_path"):
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def infer_required_flag_value(
    *,
    flag: str,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> str | None:
    normalized = flag.lstrip("-").replace("-", "_")
    candidate_keys = [
        normalized,
        flag,
        normalized.removesuffix("_path"),
        "input_path",
        "input_file",
        "file",
        "path",
        "output_path",
        "output",
        "format",
        "output_format",
        "mode",
    ]
    for key in candidate_keys:
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if normalized in {"topic", "title", "name"}:
        return goal.strip() or user_query.strip() or None
    if normalized in {"notes", "note", "description"}:
        note_text = constraints.get("notes")
        if isinstance(note_text, str) and note_text.strip():
            return note_text.strip()
        if user_query.strip() and user_query.strip() != goal.strip():
            return user_query.strip()
    return None


def flag_to_field_name(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def resolve_shell_mode(
    constraints: dict[str, Any],
    *,
    default: SkillShellMode = "conservative",
) -> SkillShellMode:
    for key in ("shell_mode", "skill_shell_mode", "shell_agent_mode"):
        if key in constraints:
            return normalize_shell_mode(constraints.get(key))
    return normalize_shell_mode(default)


def normalize_generated_files(value: Any) -> list[SkillGeneratedFile]:
    if not isinstance(value, list):
        return []
    generated_files: list[SkillGeneratedFile] = []
    for raw_item in value[:MAX_GENERATED_FILE_COUNT]:
        item = SkillGeneratedFile.from_dict(raw_item if isinstance(raw_item, dict) else None)
        if item is None:
            continue
        normalized_path = item.path.replace("\\", "/").strip()
        pure_path = PurePosixPath(normalized_path)
        if not normalized_path or pure_path.is_absolute():
            continue
        if any(part == ".." for part in pure_path.parts):
            continue
        if len(item.content.encode("utf-8")) > MAX_GENERATED_FILE_BYTES:
            continue
        generated_files.append(
            SkillGeneratedFile(
                path=str(pure_path),
                content=item.content,
                description=item.description,
            )
        )
    return generated_files


def load_script_previews(
    loaded_skill: LoadedSkill,
    *,
    query_text: str,
    limit: int = 4,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for relative_path in iter_runnable_scripts(loaded_skill):
        absolute_path = (loaded_skill.skill.path / relative_path).resolve()
        try:
            content = absolute_path.read_text(encoding="utf-8")
        except Exception:
            continue
        previews.append(
            {
                "path": relative_path,
                "preview": _truncate_text(content, max_chars=MAX_SCRIPT_PREVIEW_CHARS),
                "score": _score_relevance(relative_path + "\n" + content, query_text),
            }
        )
    ranked = sorted(previews, key=lambda item: (item["score"], len(item["preview"])), reverse=True)
    return ranked[: max(1, limit)]


def _clone_plan_with_shell_mode(
    plan: SkillCommandPlan,
    *,
    shell_mode: SkillShellMode,
) -> SkillCommandPlan:
    return SkillCommandPlan(
        skill_name=plan.skill_name,
        goal=plan.goal,
        user_query=plan.user_query,
        runtime_target=plan.runtime_target,
        constraints=dict(plan.constraints),
        command=plan.command,
        mode=plan.mode,
        shell_mode=shell_mode,
        rationale=plan.rationale,
        entrypoint=plan.entrypoint,
        cli_args=list(plan.cli_args),
        generated_files=list(plan.generated_files),
        missing_fields=list(plan.missing_fields),
        failure_reason=plan.failure_reason,
        hints=dict(plan.hints),
    )


class SkillCommandPlanner:
    def __init__(
        self,
        *,
        llm_client: SkillPlannerLLMClient | None = None,
        default_shell_mode: SkillShellMode = "conservative",
        default_runtime_target: SkillRuntimeTarget | None = None,
    ):
        self.llm_client = llm_client
        self.default_shell_mode = normalize_shell_mode(default_shell_mode)
        self.default_runtime_target = default_runtime_target or SkillRuntimeTarget.linux_default()

    async def plan(
        self,
        *,
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
    ) -> SkillCommandPlan:
        constraints = dict(request.constraints or {})
        shell_mode = resolve_shell_mode(constraints, default=self.default_shell_mode)
        runtime_target = SkillRuntimeTarget.from_dict(
            request.runtime_target.to_dict(),
            default=self.default_runtime_target,
        )
        normalized_request = SkillExecutionRequest(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            workspace=request.workspace,
            shell_mode=shell_mode,
            runtime_target=runtime_target,
            constraints=constraints,
        )
        shell_hints = build_shell_hints(loaded_skill)
        explicit_command = extract_requested_shell_command(constraints)
        if explicit_command:
            return SkillCommandPlan(
                skill_name=normalized_request.skill_name,
                goal=normalized_request.goal,
                user_query=normalized_request.user_query,
                runtime_target=normalized_request.runtime_target,
                constraints=constraints,
                command=explicit_command,
                mode="explicit",
                shell_mode=shell_mode,
                rationale="Used the explicit shell command provided in constraints.",
                hints=shell_hints,
            )

        conservative_plan = self._plan_conservative(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
        )
        if shell_mode != "free_shell":
            return conservative_plan
        if not conservative_plan.is_manual_required:
            return _clone_plan_with_shell_mode(
                conservative_plan,
                shell_mode="free_shell",
            )
        return await self._plan_free_shell(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
            fallback_plan=conservative_plan,
        )

    def _plan_conservative(
        self,
        *,
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        shell_hints: dict[str, Any],
    ) -> SkillCommandPlan:
        constraints = dict(request.constraints or {})
        required_credentials = shell_hints.get("required_credentials", [])
        if required_credentials and not has_credential_values(constraints):
            return self._manual_required_plan(
                request=request,
                shell_hints=shell_hints,
                failure_reason="missing_credential",
                missing_fields=[str(item).lower() for item in required_credentials],
                rationale=(
                    "The skill documentation indicates credentials are required before command "
                    "planning can continue safely."
                ),
            )

        if loaded_skill.skill.name.strip().lower() == "xlsx":
            xlsx_plan = self._plan_xlsx(
                loaded_skill=loaded_skill,
                request=request,
                shell_hints=shell_hints,
            )
            if xlsx_plan is not None:
                return xlsx_plan

        input_paths = extract_input_paths(constraints)
        if len(input_paths) > 1:
            return self._manual_required_plan(
                request=request,
                shell_hints=shell_hints,
                failure_reason="ambiguous_target_file",
                missing_fields=["input_path"],
                rationale=(
                    "Multiple input paths were provided, so the target file needs to be "
                    "disambiguated before generating a shell command."
                ),
            )

        runnable_scripts = list(shell_hints.get("runnable_scripts", []))
        if len(runnable_scripts) == 1:
            entrypoint = runnable_scripts[0]
            structured_cli_args = extract_structured_cli_args(constraints)
            if structured_cli_args is not None:
                return SkillCommandPlan(
                    skill_name=request.skill_name,
                    goal=request.goal,
                    user_query=request.user_query,
                    runtime_target=request.runtime_target,
                    constraints=constraints,
                    command=build_portable_script_command(
                        entrypoint,
                        structured_cli_args,
                        runtime_target=request.runtime_target,
                    ),
                    mode="structured_args",
                    shell_mode=request.shell_mode,
                    rationale=(
                        "Built the command from structured CLI arguments for the skill's "
                        "single runnable entrypoint."
                    ),
                    entrypoint=entrypoint,
                    cli_args=structured_cli_args,
                    hints=shell_hints,
                )

            required_flags = extract_required_cli_flags(loaded_skill.skill.path / entrypoint)
            inferred_cli_args: list[str] = []
            missing_fields: list[str] = []
            for flag in required_flags:
                inferred_value = infer_required_flag_value(
                    flag=flag,
                    goal=request.goal,
                    user_query=request.user_query,
                    constraints=constraints,
                )
                if inferred_value is None:
                    missing_fields.append(flag_to_field_name(flag))
                    continue
                inferred_cli_args.extend([flag, inferred_value])
            optional_notes = infer_required_flag_value(
                flag="--notes",
                goal=request.goal,
                user_query=request.user_query,
                constraints=constraints,
            )
            if optional_notes and "--notes" not in inferred_cli_args:
                inferred_cli_args.extend(["--notes", optional_notes])

            if missing_fields:
                failure_reason = (
                    "missing_input_path"
                    if "input_path" in missing_fields
                    else "missing_required_args"
                )
                return self._manual_required_plan(
                    request=request,
                    shell_hints=shell_hints,
                    failure_reason=failure_reason,
                    missing_fields=missing_fields,
                    rationale=(
                        "The skill has a single runnable entrypoint, but required arguments are "
                        "still missing so the command cannot be generated conservatively."
                    ),
                )

            return SkillCommandPlan(
                skill_name=request.skill_name,
                goal=request.goal,
                user_query=request.user_query,
                runtime_target=request.runtime_target,
                constraints=constraints,
                command=build_portable_script_command(
                    entrypoint,
                    inferred_cli_args,
                    runtime_target=request.runtime_target,
                ),
                mode="inferred",
                shell_mode=request.shell_mode,
                rationale=(
                    "Inferred the command from the skill's single runnable entrypoint and the "
                    "available structured request context."
                ),
                entrypoint=entrypoint,
                cli_args=inferred_cli_args,
                hints=shell_hints,
            )

        failure_reason = "manual_command_required"
        rationale = (
            "The skill needs more execution detail before a safe shell command can be planned."
        )
        if not runnable_scripts:
            failure_reason = "environment_not_prepared"
            rationale = (
                "No runnable skill entrypoint was discovered, so the runtime needs explicit "
                "execution guidance or environment preparation."
            )
        return self._manual_required_plan(
            request=request,
            shell_hints=shell_hints,
            failure_reason=failure_reason,
            missing_fields=[],
            rationale=rationale,
        )

    async def _plan_free_shell(
        self,
        *,
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        shell_hints: dict[str, Any],
        fallback_plan: SkillCommandPlan,
    ) -> SkillCommandPlan:
        if self.llm_client is None or not self.llm_client.is_available():
            return self._manual_required_plan(
                request=request,
                shell_hints={
                    **shell_hints,
                    "planner": "free_shell",
                    "warnings": ["No utility LLM is available for free-shell planning."],
                },
                failure_reason="llm_not_available",
                missing_fields=[],
                rationale=(
                    "Free-shell planning requires an available utility LLM because the skill "
                    "cannot be reduced to the conservative single-entrypoint planner."
                ),
            )

        query_text = "\n".join(
            [
                request.goal,
                request.user_query,
                " ".join(str(item) for item in request.constraints.values()),
            ]
        )
        python_examples = select_relevant_examples(
            extract_python_examples(loaded_skill.skill_md),
            query_text=query_text,
            limit=4,
        )
        script_previews = load_script_previews(
            loaded_skill,
            query_text=query_text,
            limit=4,
        )
        skill_md_excerpt = _truncate_text(
            loaded_skill.skill_md,
            max_chars=MAX_MARKDOWN_EXCERPT_CHARS,
        )
        system_prompt, user_prompt = build_skill_free_shell_planner_prompt(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            runtime_target=request.runtime_target.to_dict(),
            constraints=request.constraints,
            shell_hints=shell_hints,
            file_inventory=[item.to_dict() for item in loaded_skill.file_inventory],
            skill_md_excerpt=skill_md_excerpt,
            script_previews=script_previews,
            python_examples=python_examples,
        )
        try:
            payload = await self.llm_client.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=1800,
            )
        except Exception as exc:
            return self._manual_required_plan(
                request=request,
                shell_hints={
                    **shell_hints,
                    "planner": "free_shell",
                    "warnings": [f"Free-shell planning failed: {exc}"],
                },
                failure_reason="llm_planning_failed",
                missing_fields=list(fallback_plan.missing_fields),
                rationale=(
                    "The free-shell planner could not produce a valid command plan, so the run "
                    "still requires manual guidance."
                ),
            )

        generated_files = normalize_generated_files(payload.get("generated_files"))
        raw_mode = str(payload.get("mode", "")).strip().lower()
        if raw_mode not in {"free_shell", "generated_script", "manual_required"}:
            raw_mode = "generated_script" if generated_files else "free_shell"
        command = normalize_shell_command(payload.get("command"))
        command = normalize_generated_command(command, generated_files)
        if command is None and generated_files:
            command = default_generated_file_command(
                generated_files,
                runtime_target=request.runtime_target,
            )
            if command and raw_mode != "manual_required":
                raw_mode = "generated_script"

        missing_fields = [
            str(item)
            for item in (payload.get("missing_fields") if isinstance(payload.get("missing_fields"), list) else [])
            if isinstance(item, (str, int, float))
        ]
        failure_reason = (
            str(payload.get("failure_reason")).strip()
            if isinstance(payload.get("failure_reason"), str)
            and payload.get("failure_reason", "").strip()
            else None
        )
        rationale = str(payload.get("rationale", "")).strip() or (
            "Planned a free-shell execution path from the skill documentation and request."
        )
        required_tools = [
            str(item)
            for item in (payload.get("required_tools") if isinstance(payload.get("required_tools"), list) else [])
            if isinstance(item, (str, int, float))
        ]
        warnings = [
            str(item)
            for item in (payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
            if isinstance(item, (str, int, float))
        ]
        hints = {
            **shell_hints,
            "planner": "free_shell",
            "required_tools": required_tools,
            "warnings": warnings,
            "script_previews": script_previews,
            "python_examples": python_examples,
        }

        if raw_mode == "manual_required" or command is None:
            return self._manual_required_plan(
                request=request,
                shell_hints=hints,
                failure_reason=failure_reason or "manual_command_required",
                missing_fields=missing_fields,
                rationale=rationale,
            )
        if (
            not request.runtime_target.supports_python
            and any(item.path.lower().endswith(".py") for item in generated_files)
        ):
            return self._manual_required_plan(
                request=request,
                shell_hints={
                    **hints,
                    "warnings": [
                        *warnings,
                        "The declared runtime target does not support Python execution.",
                    ],
                },
                failure_reason="python_not_supported",
                missing_fields=[],
                rationale=(
                    "The generated free-shell plan requires Python, but the runtime target "
                    "explicitly disallows Python execution."
                ),
            )

        return SkillCommandPlan(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            runtime_target=request.runtime_target,
            constraints=dict(request.constraints),
            command=command,
            mode=("generated_script" if generated_files else "free_shell"),
            shell_mode="free_shell",
            rationale=rationale,
            generated_files=generated_files,
            missing_fields=missing_fields,
            failure_reason=failure_reason,
            hints=hints,
        )

    def _plan_xlsx(
        self,
        *,
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        shell_hints: dict[str, Any],
    ) -> SkillCommandPlan | None:
        constraints = dict(request.constraints or {})
        input_paths = extract_input_paths(constraints)
        if len(input_paths) > 1:
            return self._manual_required_plan(
                request=request,
                shell_hints=shell_hints,
                failure_reason="ambiguous_target_file",
                missing_fields=["input_path"],
                rationale=(
                    "The xlsx skill needs one target workbook path before the recalc workflow "
                    "can be planned."
                ),
            )

        operation = str(constraints.get("operation", "")).strip().lower()
        if not operation:
            combined_text = f"{request.goal}\n{request.user_query}".lower()
            if re.search(
                r"(recalc|recalculate|recalculation|formula error|formula errors|#ref!|#div/0!|#value!|#name\?|重算|重新计算|公式错误|公式重算)",
                combined_text,
                re.IGNORECASE,
            ):
                operation = "recalc"

        if operation != "recalc":
            return None

        if not input_paths:
            return self._manual_required_plan(
                request=request,
                shell_hints=shell_hints,
                failure_reason="missing_input_path",
                missing_fields=["input_path"],
                rationale=(
                    "The xlsx recalc workflow needs the workbook path before the command can be "
                    "planned."
                ),
            )

        entrypoint = "scripts/recalc.py"
        cli_args = [input_paths[0]]
        return SkillCommandPlan(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            runtime_target=request.runtime_target,
            constraints=constraints,
            command=build_portable_script_command(
                entrypoint,
                cli_args,
                runtime_target=request.runtime_target,
            ),
            mode="inferred",
            shell_mode=request.shell_mode,
            rationale="Matched the xlsx recalc workflow and targeted the bundled recalc script.",
            entrypoint=entrypoint,
            cli_args=cli_args,
            hints=shell_hints,
        )

    def _manual_required_plan(
        self,
        *,
        request: SkillExecutionRequest,
        shell_hints: dict[str, Any],
        failure_reason: str,
        missing_fields: list[str],
        rationale: str,
    ) -> SkillCommandPlan:
        return SkillCommandPlan(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            runtime_target=request.runtime_target,
            constraints=dict(request.constraints),
            command=None,
            mode="manual_required",
            shell_mode=request.shell_mode,
            rationale=rationale,
            missing_fields=list(missing_fields),
            failure_reason=failure_reason,
            hints=dict(shell_hints),
        )
