from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

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
DEFAULT_SCRIPT_TIMEOUT_S = int(os.environ.get("MCP_SCRIPT_TIMEOUT_S", "120"))
DEFAULT_RUN_TIMEOUT_S = int(os.environ.get("MCP_RUN_TIMEOUT_S", "300"))
MAX_REFERENCE_BYTES = int(os.environ.get("MCP_MAX_REFERENCE_BYTES", "200000"))
MAX_LOG_PREVIEW_BYTES = int(os.environ.get("MCP_MAX_LOG_PREVIEW_BYTES", "12000"))

mcp = FastMCP("SkillRuntimeService", json_response=True)
RUN_STORE: dict[str, dict[str, Any]] = {}


class SkillServerError(RuntimeError):
    pass


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
        if suffix in {".py", ".sh", ".bash"} or os.access(path, os.X_OK):
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
    else:
        argv = [str(script_path)]
    argv.extend(str(item) for item in (cli_args or []))
    return _shell_join(argv)


def _extract_shell_examples(body: str) -> list[str]:
    examples: list[str] = []
    fenced_pattern = re.compile(r"```(?:bash|sh|shell)\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
    for match in fenced_pattern.finditer(body):
        block = match.group(1).strip()
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            examples.append(stripped)

    if not examples:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("python ", "/bin/sh ", "bash ", "./scripts/", "scripts/")):
                examples.append(stripped)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for command in examples:
        if command in seen:
            continue
        seen.add(command)
        deduplicated.append(command)
    return deduplicated


def _build_shell_hints(
    *,
    skill_dir: Path,
    metadata: dict[str, Any],
    body: str,
) -> dict[str, Any]:
    example_commands = _extract_shell_examples(body)
    runnable_scripts = _iter_runnable_scripts(skill_dir)
    metadata_command = metadata.get("shell_command") or metadata.get("command")
    explicit_commands = list(example_commands)
    if isinstance(metadata_command, str) and metadata_command.strip():
        explicit_commands.insert(0, metadata_command.strip())

    deduplicated_commands: list[str] = []
    seen: set[str] = set()
    for command in explicit_commands:
        if command in seen:
            continue
        seen.add(command)
        deduplicated_commands.append(command)

    auto_generated_commands = [
        _default_shell_command(relative_path) for relative_path in runnable_scripts[:3]
    ]

    return {
        "execution_mode": "shell",
        "example_commands": deduplicated_commands[:5],
        "auto_generated_commands": auto_generated_commands,
        "runnable_scripts": runnable_scripts,
        "notes": (
            "Provide constraints.shell_command / constraints.command for an explicit shell task, "
            "or provide constraints.args / constraints.cli_args when the skill has a single "
            "runnable entrypoint so the runtime can build the shell command conservatively."
        ),
    }


def _normalize_shell_command(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        argv = [str(item) for item in value if isinstance(item, (str, int, float))]
        if argv:
            return _shell_join(argv)
    return None


def _extract_requested_shell_command(constraints: dict[str, Any] | None) -> str | None:
    if not isinstance(constraints, dict):
        return None
    for key in ("shell_command", "command"):
        command = _normalize_shell_command(constraints.get(key))
        if command:
            return command
    return None


def _is_dry_run(constraints: dict[str, Any] | None) -> bool:
    if not isinstance(constraints, dict):
        return False
    return bool(constraints.get("dry_run") or constraints.get("plan_only"))


def _normalize_cli_args(value: Any) -> list[str] | None:
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


def _extract_structured_cli_args(constraints: dict[str, Any] | None) -> list[str] | None:
    if not isinstance(constraints, dict):
        return None
    for key in ("cli_args", "args", "command_args"):
        normalized = _normalize_cli_args(constraints.get(key))
        if normalized is not None:
            return normalized
    return None


def _extract_required_cli_flags(script_path: Path) -> list[str]:
    try:
        raw = script_path.read_text(encoding="utf-8")
    except Exception:
        return []
    pattern = re.compile(
        r"add_argument\(\s*['\"](?P<flag>--[a-zA-Z0-9_-]+)['\"][^)]*required\s*=\s*True",
        re.DOTALL,
    )
    return [match.group("flag") for match in pattern.finditer(raw)]


def _infer_required_flag_value(
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


def _plan_shell_command(
    *,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    explicit_command = _extract_requested_shell_command(constraints)
    if explicit_command is not None:
        return explicit_command, {
            "mode": "explicit_shell_command",
            "command": explicit_command,
        }

    shell_hints = (
        skill_payload.get("shell_hints")
        if isinstance(skill_payload.get("shell_hints"), dict)
        else {}
    )
    skill_name = str(skill_payload.get("name", "")).strip().lower()
    if skill_name == "xlsx":
        planned_command, xlsx_plan = _plan_xlsx_shell_command(
            skill_payload=skill_payload,
            goal=goal,
            user_query=user_query,
            constraints=constraints,
        )
        if planned_command is not None or xlsx_plan.get("mode") != "needs_shell_command":
            return planned_command, xlsx_plan

    runnable_scripts = [
        str(item)
        for item in shell_hints.get("runnable_scripts", [])
        if isinstance(item, str) and item.strip()
    ]
    structured_cli_args = _extract_structured_cli_args(constraints)
    skill_dir = Path(skill_payload["path"]).resolve()

    if len(runnable_scripts) == 1:
        relative_script = runnable_scripts[0]
        if structured_cli_args is not None:
            command = _build_script_shell_command(
                skill_dir=skill_dir,
                relative_script=relative_script,
                cli_args=structured_cli_args,
            )
            return command, {
                "mode": "single_runnable_script_with_structured_args",
                "script": relative_script,
                "cli_args": structured_cli_args,
                "command": command,
            }

        required_flags = _extract_required_cli_flags(skill_dir / relative_script)
        if not required_flags:
            command = _build_script_shell_command(
                skill_dir=skill_dir,
                relative_script=relative_script,
                cli_args=[],
            )
            return command, {
                "mode": "single_runnable_script_no_required_args",
                "script": relative_script,
                "command": command,
            }

        inferred_cli_args: list[str] = []
        unresolved_flags: list[str] = []
        for flag in required_flags:
            value = _infer_required_flag_value(
                flag=flag,
                goal=goal,
                user_query=user_query,
                constraints=constraints,
            )
            if value is None:
                unresolved_flags.append(flag)
                continue
            inferred_cli_args.extend([flag, value])
        if not unresolved_flags:
            optional_notes = _infer_required_flag_value(
                flag="--notes",
                goal=goal,
                user_query=user_query,
                constraints=constraints,
            )
            if optional_notes and "--notes" not in inferred_cli_args:
                inferred_cli_args.extend(["--notes", optional_notes])
            command = _build_script_shell_command(
                skill_dir=skill_dir,
                relative_script=relative_script,
                cli_args=inferred_cli_args,
            )
            return command, {
                "mode": "single_runnable_script_inferred_required_args",
                "script": relative_script,
                "required_flags": required_flags,
                "cli_args": inferred_cli_args,
                "command": command,
            }

        return None, {
            "mode": "single_runnable_script_missing_required_args",
            "script": relative_script,
            "required_flags": required_flags,
            "missing_required_flags": unresolved_flags,
            "suggested_command": _build_script_shell_command(
                skill_dir=skill_dir,
                relative_script=relative_script,
                cli_args=[],
            ),
        }

    return None, {
        "mode": "needs_shell_command",
        "runnable_scripts": runnable_scripts,
        "example_commands": shell_hints.get("example_commands", []),
        "auto_generated_commands": shell_hints.get("auto_generated_commands", []),
    }


def _extract_input_path(constraints: dict[str, Any]) -> str | None:
    for key in ("input_path", "file_path", "path"):
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    paths = constraints.get("input_paths")
    if isinstance(paths, list):
        for item in paths:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _plan_xlsx_shell_command(
    *,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    operation = str(constraints.get("operation", "")).strip().lower()
    if not operation:
        combined_text = f"{goal}\n{user_query}".lower()
        if re.search(
            r"(recalc|recalculate|recalculation|formula error|formula errors|#ref!|#div/0!|#value!|#name\?|重算|重新计算|公式错误|公式重算)",
            combined_text,
            re.IGNORECASE,
        ):
            operation = "recalc"

    input_path = _extract_input_path(constraints)
    if operation == "recalc":
        if not input_path:
            return None, {
                "mode": "xlsx_recalc_missing_input_path",
                "operation": "recalc",
                "script": "scripts/recalc.py",
                "missing_required_fields": ["input_path"],
            }
        skill_dir = Path(skill_payload["path"]).resolve()
        command = _build_script_shell_command(
            skill_dir=skill_dir,
            relative_script="scripts/recalc.py",
            cli_args=[input_path],
        )
        return command, {
            "mode": "xlsx_recalc_from_constraints",
            "operation": "recalc",
            "script": "scripts/recalc.py",
            "input_path": input_path,
            "command": command,
        }

    return None, {
        "mode": "needs_shell_command",
        "runnable_scripts": (
            skill_payload.get("shell_hints", {}).get("runnable_scripts", [])
            if isinstance(skill_payload.get("shell_hints"), dict)
            else []
        ),
    }


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
    shell_hints = _build_shell_hints(skill_dir=skill_dir, metadata=metadata, body=body)
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
    shell_hints = _build_shell_hints(skill_dir=skill_dir, metadata=metadata, body=body)

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
        RUN_STORE[run_id] = record


async def _run_shell_task(
    *,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
    shell_command: str,
    shell_plan: dict[str, Any] | None,
    timeout_s: int,
    cleanup_workspace: bool,
) -> dict[str, Any]:
    skill_dir = Path(skill_payload["path"]).resolve()
    workspace_dir = Path(
        tempfile.mkdtemp(prefix=f"{skill_payload['name']}-", dir=str(WORKSPACE_ROOT))
    ).resolve()
    request_file = _write_skill_request(
        workspace_dir=workspace_dir,
        skill_payload=skill_payload,
        goal=goal,
        user_query=user_query,
        constraints=constraints,
    )
    env = _build_run_env(
        skill_dir=skill_dir,
        workspace_dir=workspace_dir,
        goal=goal,
        user_query=user_query,
        constraints=constraints,
        request_file=request_file,
    )
    run_id = f"skill-run-{uuid.uuid4().hex}"

    try:
        process = await asyncio.create_subprocess_exec(
            *_build_shell_exec_argv(shell_command),
            cwd=str(workspace_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, int(timeout_s)),
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        stdout_preview, stdout_truncated = _truncate_log(stdout_text)
        stderr_preview, stderr_truncated = _truncate_log(stderr_text)
        artifacts = _collect_workspace_artifacts(workspace_dir)
        exit_code = (
            124 if timed_out and process.returncode in {None, 0} else process.returncode
        )
        success = (exit_code == 0) and not timed_out
        status = "completed" if success else ("timed_out" if timed_out else "failed")
        record = {
            "run_id": run_id,
            "skill_name": skill_payload["name"],
            "status": status,
            "success": success,
            "execution_mode": "shell",
            "command": shell_command,
            "workspace": str(workspace_dir),
            "request_file": str(request_file),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "logs": {
                "stdout": stdout_text,
                "stderr": stderr_text,
            },
            "logs_preview": {
                "stdout": stdout_preview,
                "stderr": stderr_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
            "artifacts": artifacts,
        }
        _store_run_record(record)
        summary = (
            f"Executed shell command for skill '{skill_payload['name']}'"
            if success
            else f"Shell execution for skill '{skill_payload['name']}' finished with status {status}"
        )
        return {
            "summary": summary,
            "status": status,
            "success": success,
            "execution_mode": "shell",
            "run_id": run_id,
            "command": shell_command,
            "workspace": str(workspace_dir),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "request_file": str(request_file),
            "artifacts": artifacts,
            "logs_preview": record["logs_preview"],
            "shell_plan": shell_plan or {},
            "skill": {
                "name": skill_payload["name"],
                "description": skill_payload["description"],
                "tags": skill_payload["tags"],
                "path": skill_payload["path"],
            },
            "shell_hints": skill_payload["shell_hints"],
        }
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_dir, ignore_errors=True)


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
    constraints: dict[str, Any] | None = None,
    timeout_s: int = DEFAULT_RUN_TIMEOUT_S,
    cleanup_workspace: bool = False,
) -> dict[str, Any]:
    """Execute a skill task through a shell-oriented runtime boundary."""
    payload = _load_skill_payload(skill_name)
    normalized_constraints = constraints or {}
    shell_command, shell_plan = _plan_shell_command(
        skill_payload=payload,
        goal=goal,
        user_query=user_query or "",
        constraints=normalized_constraints,
    )
    if shell_command is None:
        return {
            "summary": (
                f"Skill '{skill_name}' needs more shell execution detail before it can run"
            ),
            "status": "needs_shell_command",
            "success": False,
            "execution_mode": "shell",
            "name": payload["name"],
            "skill_name": skill_name,
            "goal": goal,
            "user_query": user_query or "",
            "constraints": normalized_constraints,
            "skill": {
                "name": payload["name"],
                "description": payload["description"],
                "tags": payload["tags"],
                "path": payload["path"],
            },
            "file_inventory": payload["file_inventory"],
            "references": payload["references"],
            "shell_hints": payload["shell_hints"],
            "shell_plan": shell_plan,
            "notes": (
                "Pass constraints.shell_command (or constraints.command), or provide "
                "structured CLI args in constraints.args / constraints.cli_args when "
                "the skill has a single runnable entrypoint."
            ),
        }
    if _is_dry_run(normalized_constraints):
        return {
            "summary": f"Planned shell task for '{skill_name}' without execution",
            "status": "planned",
            "success": True,
            "execution_mode": "shell",
            "name": payload["name"],
            "skill_name": skill_name,
            "goal": goal,
            "user_query": user_query or "",
            "constraints": normalized_constraints,
            "command": shell_command,
            "shell_plan": shell_plan,
            "skill": {
                "name": payload["name"],
                "description": payload["description"],
                "tags": payload["tags"],
                "path": payload["path"],
            },
            "file_inventory": payload["file_inventory"],
            "references": payload["references"],
            "shell_hints": payload["shell_hints"],
            "notes": "Dry run only; the shell command was planned but not executed.",
        }
    return await _run_shell_task(
        skill_payload=payload,
        goal=goal,
        user_query=user_query or "",
        constraints=normalized_constraints,
        shell_command=shell_command,
        shell_plan=shell_plan,
        timeout_s=timeout_s,
        cleanup_workspace=cleanup_workspace,
    )


@mcp.tool()
def get_run_logs(run_id: str) -> dict[str, Any]:
    """Fetch full stdout/stderr logs for a prior shell-based skill run."""
    record = RUN_STORE.get(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    logs = record.get("logs", {})
    return {
        "summary": f"Loaded logs for run '{run_id}'",
        "run_id": run_id,
        "skill_name": record.get("skill_name"),
        "status": record.get("status"),
        "success": bool(record.get("success")),
        "command": record.get("command"),
        "stdout": logs.get("stdout", ""),
        "stderr": logs.get("stderr", ""),
    }


@mcp.tool()
def get_run_artifacts(run_id: str) -> dict[str, Any]:
    """List artifacts produced by a prior shell-based skill run."""
    record = RUN_STORE.get(run_id)
    if record is None:
        raise SkillServerError(f"Unknown run_id: {run_id}")
    return {
        "summary": f"Loaded artifacts for run '{run_id}'",
        "run_id": run_id,
        "skill_name": record.get("skill_name"),
        "status": record.get("status"),
        "success": bool(record.get("success")),
        "workspace": record.get("workspace"),
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
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
