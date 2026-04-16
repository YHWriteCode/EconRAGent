from __future__ import annotations

import calendar
import os
import re
import shlex
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from kg_agent.agent.prompts import (
    build_skill_constraint_inference_prompt,
    build_skill_free_shell_planner_prompt,
)
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
MAX_MARKDOWN_EXCERPT_CHARS = 9000
MAX_GENERATED_FILE_COUNT = 5
MAX_GENERATED_FILE_BYTES = 64000
MAX_BOOTSTRAP_COMMAND_COUNT = 4
MAX_FREE_SHELL_EXAMPLE_COUNT = 8
MAX_FREE_SHELL_EXAMPLE_CHARS = 2400
MAX_FREE_SHELL_TOTAL_EXAMPLE_CHARS = 12000
INLINE_PYTHON_SCRIPT_PROMOTION_MIN_EXAMPLES = 3
INLINE_PYTHON_SCRIPT_PROMOTION_MIN_CHARS = 120
PREFERRED_GENERATED_ENTRYPOINT_NAMES = ("main.py", "run.py", "app.py", "script.py")
DATE_TOKEN_PATTERN = re.compile(
    r"\b(?P<year>20\d{2})(?:[-/.]?(?P<month>0[1-9]|1[0-2]))(?:[-/.]?(?P<day>0[1-9]|[12]\d|3[01]))\b"
)
PAREN_SYMBOL_PATTERN = re.compile(
    r"\((?P<symbol>[A-Za-z]{1,5}|\d{6})\)",
)
DIGIT_SYMBOL_PATTERN = re.compile(r"(?<!\d)(?P<symbol>\d{6})(?!\d)")
UPPERCASE_SYMBOL_PATTERN = re.compile(r"\b(?P<symbol>[A-Z]{1,5}|\d{6})\b")
RELATIVE_DATE_WINDOW_PATTERN = re.compile(
    r"(?:(?P<prefix>最近|近|过去|过去的|近来|last|past|previous)\s*"
    r"(?P<quantity>[0-9]+|一|二|两|三|四|五|六|七|八|九|十|十一|十二)?\s*"
    r"(?P<unit>年|个月|月|周|天|years?|year|months?|month|weeks?|week|days?|day))"
    r"|(?:(?P<quantity_suffix>[0-9]+|一|二|两|三|四|五|六|七|八|九|十|十一|十二)\s*"
    r"(?P<unit_suffix>年|个月|月|周|天|years?|year|months?|month|weeks?|week|days?|day)"
    r"(?P<suffix>内|以来|之内))",
    re.IGNORECASE,
)
UPPERCASE_SYMBOL_STOPWORDS = {
    "API",
    "CSV",
    "JSON",
    "HTTP",
    "HTTPS",
    "LLM",
    "MCP",
    "PDF",
    "PNG",
    "SQL",
    "TSV",
    "TXT",
    "XML",
    "YAML",
}
CODE_LIKE_FLAG_NAMES = {
    "code",
    "codes",
    "symbol",
    "symbols",
    "ticker",
    "tickers",
    "target",
    "target_code",
}
START_DATE_FLAG_NAMES = {"start", "start_date", "from", "date_from"}
END_DATE_FLAG_NAMES = {"end", "end_date", "to", "date_to"}
TREND_START_DATE_FLAG_NAMES = {"trend_start", "trend_start_date", "window_start"}
TREND_END_DATE_FLAG_NAMES = {"trend_end", "trend_end_date", "window_end"}
OPTIONAL_INFERABLE_FLAG_NAMES = (
    CODE_LIKE_FLAG_NAMES
    | START_DATE_FLAG_NAMES
    | END_DATE_FLAG_NAMES
    | TREND_START_DATE_FLAG_NAMES
    | TREND_END_DATE_FLAG_NAMES
    | {"output", "output_path", "format", "mode", "dep", "indep", "notes", "note", "description"}
)
DATE_LIKE_CONSTRAINT_NAMES = (
    START_DATE_FLAG_NAMES | END_DATE_FLAG_NAMES | TREND_START_DATE_FLAG_NAMES | TREND_END_DATE_FLAG_NAMES
)
BOOL_LIKE_CONSTRAINT_NAMES = {
    "dry_run",
    "plan_only",
    "overwrite",
    "recursive",
    "preserve_formulas",
}
PATH_LIKE_CONSTRAINT_NAMES = {
    "input_path",
    "output_path",
    "output",
    "destination_path",
    "file",
    "path",
    "input_file",
}
GENERIC_INFERABLE_CONSTRAINT_KEYS = {
    "input_path",
    "input_paths",
    "output_path",
    "output",
    "destination_path",
    "format",
    "output_format",
    "mode",
    "dry_run",
    "plan_only",
    "shell_mode",
    "notes",
    "note",
    "description",
    "operation",
    "preserve_formulas",
}
SCRIPT_FIRST_REQUEST_PATTERN = re.compile(
    r"("
    r"script\s+first|"
    r"helper\s+script|"
    r"write(?: a| the)? (?:helper |python )?script|"
    r"generate(?: a| the)? (?:helper |python )?script|"
    r"complex\s+(?:shell|command)|"
    r"pipeline|"
    r"\u5148\u5199\u811a\u672c\u518d\u6267\u884c|"
    r"\u5148\u751f\u6210\u811a\u672c|"
    r"\u5199(?:\u4e00\u4e2a|\u4e2a)?(?:helper|python)?\s*\u811a\u672c|"
    r"\u590d\u6742\u547d\u4ee4|"
    r"\u547d\u4ee4\u94fe"
    r")",
    re.IGNORECASE,
)


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


@dataclass(frozen=True)
class RelativeDateWindow:
    quantity: int
    unit: str
    start: date
    end: date
    matched_text: str


def _current_date() -> date:
    return date.today()


def _parse_small_int(value: str | None) -> int:
    normalized = str(value or "").strip()
    if not normalized:
        return 1
    if normalized.isdigit():
        return max(1, int(normalized))
    direct = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
    }
    if normalized in direct:
        return direct[normalized]
    if "十" in normalized:
        left, _, right = normalized.partition("十")
        tens = direct.get(left, 1) if left else 1
        ones = direct.get(right, 0) if right else 0
        return max(1, tens * 10 + ones)
    return 1


def _normalize_relative_unit(raw_unit: str | None) -> str | None:
    normalized = str(raw_unit or "").strip().lower()
    if normalized in {"年", "year", "years"}:
        return "years"
    if normalized in {"个月", "月", "month", "months"}:
        return "months"
    if normalized in {"周", "week", "weeks"}:
        return "weeks"
    if normalized in {"天", "day", "days"}:
        return "days"
    return None


def _shift_date_by_months(base: date, months: int) -> date:
    absolute_month = base.year * 12 + (base.month - 1) + months
    year = absolute_month // 12
    month = absolute_month % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _subtract_relative_window(reference_date: date, quantity: int, unit: str) -> date:
    if unit == "years":
        return _shift_date_by_months(reference_date, -(quantity * 12))
    if unit == "months":
        return _shift_date_by_months(reference_date, -quantity)
    if unit == "weeks":
        return reference_date - timedelta(weeks=quantity)
    if unit == "days":
        return reference_date - timedelta(days=quantity)
    return reference_date


def extract_relative_date_windows(
    text: str,
    *,
    reference_date: date | None = None,
) -> list[RelativeDateWindow]:
    end_date = reference_date or _current_date()
    windows: list[RelativeDateWindow] = []
    seen: set[tuple[int, str, date, date]] = set()
    for match in RELATIVE_DATE_WINDOW_PATTERN.finditer(text or ""):
        quantity_text = match.group("quantity") or match.group("quantity_suffix")
        unit_text = match.group("unit") or match.group("unit_suffix")
        unit = _normalize_relative_unit(unit_text)
        if unit is None:
            continue
        quantity = _parse_small_int(quantity_text)
        start_date = _subtract_relative_window(end_date, quantity, unit)
        window = RelativeDateWindow(
            quantity=quantity,
            unit=unit,
            start=start_date,
            end=end_date,
            matched_text=match.group(0).strip(),
        )
        identity = (window.quantity, window.unit, window.start, window.end)
        if identity in seen:
            continue
        seen.add(identity)
        windows.append(window)
    return windows


def _window_duration_days(window: RelativeDateWindow) -> int:
    return max(0, (window.end - window.start).days)


def _select_primary_relative_date_window(
    windows: list[RelativeDateWindow],
) -> RelativeDateWindow | None:
    if not windows:
        return None
    return max(windows, key=_window_duration_days)


def _select_secondary_relative_date_window(
    windows: list[RelativeDateWindow],
) -> RelativeDateWindow | None:
    if len(windows) < 2:
        return None
    ranked = sorted(windows, key=_window_duration_days)
    return ranked[0]


def _normalize_bool_like(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def _normalize_code_like_value(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if re.fullmatch(r"\d{6}", normalized):
        return normalized
    if re.fullmatch(r"[A-Za-z]{1,5}", normalized):
        return normalized.upper()
    return None


def _normalize_multi_code_like_value(value: Any) -> str | None:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        parts = [item.strip() for item in str(value or "").split(",") if item.strip()]
    normalized_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = _normalize_code_like_value(part)
        if normalized is None or normalized in seen:
            continue
        normalized_parts.append(normalized)
        seen.add(normalized)
    if not normalized_parts:
        return None
    return ",".join(normalized_parts)


def _normalize_simple_string(value: Any, *, upper: bool = False) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized.upper() if upper else normalized


def _build_allowed_constraint_keys(
    loaded_skill: LoadedSkill,
    shell_hints: dict[str, Any],
) -> list[str]:
    allowed: set[str] = set(GENERIC_INFERABLE_CONSTRAINT_KEYS)
    for relative_path in shell_hints.get("runnable_scripts", []):
        if not isinstance(relative_path, str) or not relative_path.strip():
            continue
        script_path = (loaded_skill.skill.path / relative_path).resolve()
        for flag in extract_cli_flags(script_path):
            normalized = flag_to_field_name(flag)
            if not normalized:
                continue
            allowed.add(normalized)
            if normalized in {"input", "file", "path"}:
                allowed.add("input_path")
            if normalized == "output":
                allowed.add("output_path")
    return sorted(allowed)


def _sanitize_inferred_constraint_value(key: str, value: Any) -> Any:
    normalized_key = str(key or "").strip().lower()
    if not normalized_key:
        return None
    if normalized_key in DATE_LIKE_CONSTRAINT_NAMES:
        candidate = str(value or "").strip()
        return candidate if re.fullmatch(r"20\d{6}", candidate) else None
    if normalized_key in {"code", "target", "target_code", "ticker", "symbol"}:
        return _normalize_code_like_value(value)
    if normalized_key in {"codes", "symbols", "tickers"}:
        return _normalize_multi_code_like_value(value)
    if normalized_key in BOOL_LIKE_CONSTRAINT_NAMES:
        return _normalize_bool_like(value)
    if normalized_key == "shell_mode":
        return normalize_shell_mode(value)
    if normalized_key == "cli_args":
        return normalize_cli_args(value)
    if normalized_key == "input_paths":
        if not isinstance(value, list):
            return None
        normalized_items = [
            item
            for item in (_normalize_simple_string(item) for item in value[:8])
            if item is not None
        ]
        return normalized_items or None
    if normalized_key in PATH_LIKE_CONSTRAINT_NAMES:
        return _normalize_simple_string(value)
    if normalized_key in {
        "format",
        "output_format",
        "mode",
        "dep",
        "indep",
        "notes",
        "note",
        "description",
        "operation",
    }:
        return _normalize_simple_string(value)
    if isinstance(value, str):
        return _normalize_simple_string(value)
    return None


def _sanitize_inferred_constraints(
    raw_constraints: Any,
    *,
    allowed_keys: set[str],
) -> dict[str, Any]:
    if not isinstance(raw_constraints, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in raw_constraints.items():
        key = str(raw_key or "").strip()
        if not key or key not in allowed_keys:
            continue
        normalized_value = _sanitize_inferred_constraint_value(key, raw_value)
        if normalized_value is None:
            continue
        sanitized[key] = normalized_value
    return sanitized


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
    max_total_chars: int | None = None,
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
    total_chars = 0
    for item in ranked:
        if len(selected) >= max(1, limit):
            break
        truncated_code = _truncate_text(str(item.get("code", "")), max_chars=max_chars)
        if (
            max_total_chars is not None
            and selected
            and total_chars + len(truncated_code) > max_total_chars
        ):
            break
        selected.append(
            {
                "language": str(item.get("language", "")).strip() or "text",
                "code": truncated_code,
            }
        )
        total_chars += len(truncated_code)
    if not selected and ranked:
        item = ranked[0]
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


def extract_documented_script_paths(markdown: str) -> list[str]:
    _, body = split_skill_markdown(markdown)
    documented: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?P<path>scripts/[A-Za-z0-9_./-]+\.(?:py|sh|bash|ps1))", body):
        path = str(match.group("path") or "").replace("\\", "/").strip()
        if not path or path in seen:
            continue
        documented.append(path)
        seen.add(path)
    return documented


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
    cli_args: list[str] | None = None,
) -> str | None:
    entrypoint = default_generated_file_entrypoint(generated_files)
    if entrypoint is None:
        return None
    return build_portable_script_command(
        entrypoint,
        cli_args,
        runtime_target=runtime_target,
    )


def default_generated_file_entrypoint(
    generated_files: list[SkillGeneratedFile],
) -> str | None:
    if not generated_files:
        return None

    def _entrypoint_rank(item: SkillGeneratedFile) -> tuple[int, int, int, int, str]:
        pure_path = PurePosixPath(item.path.replace("\\", "/").strip())
        name = pure_path.name.lower()
        suffix = pure_path.suffix.lower()
        is_preferred_name = 0 if name in PREFERRED_GENERATED_ENTRYPOINT_NAMES else 1
        suffix_rank = (
            0
            if suffix == ".py"
            else 1
            if suffix in {".sh", ".bash", ".ps1"}
            else 2
        )
        workspace_hint_rank = 0 if ".skill_generated" in str(pure_path).lower() else 1
        depth_rank = len(pure_path.parts)
        return (is_preferred_name, suffix_rank, workspace_hint_rank, depth_rank, str(pure_path))

    best = min(generated_files, key=_entrypoint_rank)
    return best.path


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
    documented_scripts = extract_documented_script_paths(loaded_skill.skill_md)
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
        "documented_scripts": documented_scripts,
        "python_example_count": len(extract_python_examples(loaded_skill.skill_md)),
        "required_credentials": required_credentials,
        "notes": (
            "Provide constraints.shell_command / constraints.command for an explicit shell task, "
            "or provide structured CLI args in constraints.args / constraints.cli_args when the skill "
            "has a single runnable entrypoint. Set constraints.shell_mode='free_shell' to enable "
            "LLM-driven shell planning, optional generated scripts, and optional bootstrap_commands "
            "before the main command."
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


def normalize_shell_commands(
    value: Any,
    *,
    limit: int = MAX_BOOTSTRAP_COMMAND_COUNT,
) -> list[str]:
    if not isinstance(value, list):
        return []
    commands: list[str] = []
    for raw_item in value[: max(1, limit)]:
        command = normalize_shell_command(raw_item)
        if not command:
            continue
        commands.append(command)
    return commands


def _is_python_command_name(value: str) -> bool:
    command_name = Path(str(value or "").strip()).name.lower()
    return command_name in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"} or (
        command_name.startswith("python3.")
        and all(part.isdigit() for part in command_name.removeprefix("python3.").split("."))
    )


def extract_inline_python_command(
    command: str | None,
) -> tuple[str, list[str]] | None:
    normalized = normalize_shell_command(command)
    if not normalized:
        return None
    for posix_mode in (True, False):
        try:
            argv = shlex.split(normalized, posix=posix_mode)
        except ValueError:
            continue
        if len(argv) < 3 or not _is_python_command_name(argv[0]) or argv[1] != "-c":
            continue
        code = str(argv[2]).strip()
        if not code:
            continue
        return code, [str(item) for item in argv[3:]]
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


def _extract_add_argument_blocks(script_path: Path) -> list[str]:
    try:
        raw = script_path.read_text(encoding="utf-8")
    except Exception:
        return []
    return [
        match.group(1)
        for match in re.finditer(r"add_argument\((.*?)\)", raw, re.DOTALL)
    ]


def extract_cli_flags(script_path: Path) -> list[str]:
    flags: list[str] = []
    seen: set[str] = set()
    for block in _extract_add_argument_blocks(script_path):
        for flag in re.findall(r"['\"](--[a-zA-Z0-9_-]+)['\"]", block):
            if flag in seen:
                continue
            flags.append(flag)
            seen.add(flag)
    return flags


def extract_cli_flag_defaults(script_path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for block in _extract_add_argument_blocks(script_path):
        flags = re.findall(r"['\"](--[a-zA-Z0-9_-]+)['\"]", block)
        if not flags:
            continue
        match = re.search(
            r"default\s*=\s*(?P<value>'[^']*'|\"[^\"]*\"|[A-Za-z0-9_./,:+-]+)",
            block,
        )
        if match is None:
            continue
        raw_value = match.group("value").strip()
        if (
            len(raw_value) >= 2
            and raw_value[0] == raw_value[-1]
            and raw_value[0] in {"'", '"'}
        ):
            raw_value = raw_value[1:-1]
        elif raw_value.endswith(","):
            raw_value = raw_value[:-1]
        defaults[flags[0]] = raw_value
    return defaults


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


def extract_date_candidates(text: str) -> list[str]:
    dates: list[str] = []
    seen: set[str] = set()
    for match in DATE_TOKEN_PATTERN.finditer(text or ""):
        candidate = (
            f"{match.group('year')}{match.group('month')}{match.group('day')}"
        )
        if candidate in seen:
            continue
        dates.append(candidate)
        seen.add(candidate)
    return dates


def extract_symbol_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in PAREN_SYMBOL_PATTERN.finditer(text or ""):
        symbol = str(match.group("symbol") or "").strip()
        if not symbol:
            continue
        normalized = symbol.upper() if symbol.isalpha() else symbol
        if normalized in seen:
            continue
        candidates.append(normalized)
        seen.add(normalized)
    for match in DIGIT_SYMBOL_PATTERN.finditer(text or ""):
        symbol = str(match.group("symbol") or "").strip()
        if not symbol:
            continue
        if symbol in seen:
            continue
        candidates.append(symbol)
        seen.add(symbol)
    for match in UPPERCASE_SYMBOL_PATTERN.finditer(text or ""):
        symbol = str(match.group("symbol") or "").strip()
        if not symbol:
            continue
        normalized = symbol.upper() if symbol.isalpha() else symbol
        if normalized in UPPERCASE_SYMBOL_STOPWORDS or normalized in seen:
            continue
        candidates.append(normalized)
        seen.add(normalized)
    return candidates


def _split_code_like_values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\s,]+", value) if item.strip()]


def _is_numeric_code_value(value: str) -> bool:
    values = _split_code_like_values(value)
    return bool(values) and all(re.fullmatch(r"\d{6}", item) for item in values)


def script_expects_numeric_codes(script_path: Path) -> bool:
    try:
        raw = script_path.read_text(encoding="utf-8")
    except Exception:
        return False
    lowered = raw.lower()
    return any(
        marker in lowered
        for marker in (
            "zfill(6)",
            "6 位纯数字",
            "6位纯数字",
            "纯数字字符串",
            "stock_zh_a_hist(",
            "symbol=_normalize_code",
        )
    )


def _merge_csv_values(existing: str, extra_value: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing.split(","), extra_value]:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
    return ",".join(merged)


def infer_script_cli_args(
    *,
    script_path: Path,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> tuple[list[str], list[str], int]:
    flags = extract_cli_flags(script_path)
    if not flags:
        return [], [], 0
    required_flags = set(extract_required_cli_flags(script_path))
    defaults = extract_cli_flag_defaults(script_path)
    inferred: list[str] = []
    missing_fields: list[str] = []
    inferred_flag_count = 0
    used_flags: set[str] = set()
    inferred_values: dict[str, str] = {}
    expects_numeric_codes = script_expects_numeric_codes(script_path)

    for flag in flags:
        normalized_flag = flag_to_field_name(flag)
        should_attempt = flag in required_flags or normalized_flag in OPTIONAL_INFERABLE_FLAG_NAMES
        if not should_attempt:
            continue
        value = infer_required_flag_value(
            flag=flag,
            goal=goal,
            user_query=user_query,
            constraints=constraints,
        )
        if value is None:
            if flag in required_flags:
                missing_fields.append(flag_to_field_name(flag))
            continue
        if normalized_flag in CODE_LIKE_FLAG_NAMES and expects_numeric_codes and not _is_numeric_code_value(value):
            return [], ["unsupported_target_for_shipped_script"], 0
        if (
            flag == "--codes"
            and expects_numeric_codes
            and _is_numeric_code_value(value)
            and defaults.get("--codes")
        ):
            value = _merge_csv_values(defaults["--codes"], value)
        inferred.extend([flag, value])
        inferred_flag_count += 1
        used_flags.add(flag)
        inferred_values[flag] = value

    target_like_value = (
        inferred_values.get("--target")
        or inferred_values.get("--code")
        or inferred_values.get("--symbol")
        or inferred_values.get("--ticker")
    )
    if (
        "--codes" in flags
        and "--codes" not in used_flags
        and target_like_value
        and _is_numeric_code_value(target_like_value)
    ):
        existing_codes = defaults.get("--codes")
        merged_codes = (
            _merge_csv_values(existing_codes, target_like_value)
            if existing_codes
            else target_like_value
        )
        inferred.extend(["--codes", merged_codes])
        inferred_flag_count += 1
        used_flags.add("--codes")

    output_path = extract_output_path(constraints)
    if output_path and "--output" in flags and "--output" not in used_flags:
        inferred.extend(["--output", output_path])
        inferred_flag_count += 1
        used_flags.add("--output")

    return inferred, missing_fields, inferred_flag_count


def infer_required_flag_value(
    *,
    flag: str,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> str | None:
    normalized = flag.lstrip("-").replace("-", "_")
    combined_text = f"{goal}\n{user_query}"
    relative_windows = extract_relative_date_windows(combined_text)
    candidate_keys = [normalized, flag, normalized.removesuffix("_path")]
    if normalized in CODE_LIKE_FLAG_NAMES:
        candidate_keys.extend(["target", "target_code", "code", "codes", "ticker", "symbol"])
    elif normalized in {"input", "input_path", "input_file", "file", "path"}:
        candidate_keys.extend(["input_path", "input_file", "file", "path"])
    elif normalized in {"output", "output_path", "destination_path"}:
        candidate_keys.extend(["output_path", "output", "destination_path"])
    elif normalized in {"format", "output_format"}:
        candidate_keys.extend(["format", "output_format"])
    elif normalized == "mode":
        candidate_keys.append("mode")
    elif normalized in TREND_START_DATE_FLAG_NAMES:
        candidate_keys.extend(["trend_start", "trend_start_date", "window_start"])
    elif normalized in TREND_END_DATE_FLAG_NAMES:
        candidate_keys.extend(["trend_end", "trend_end_date", "window_end"])
    for key in candidate_keys:
        value = constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            normalized_items = [str(item).strip() for item in value if str(item).strip()]
            if normalized_items and normalized in {"codes", "symbols", "tickers"}:
                return ",".join(normalized_items)
    if normalized in START_DATE_FLAG_NAMES:
        dates = extract_date_candidates(combined_text)
        if dates:
            return dates[0]
        primary_window = _select_primary_relative_date_window(relative_windows)
        return primary_window.start.strftime("%Y%m%d") if primary_window else None
    if normalized in END_DATE_FLAG_NAMES:
        dates = extract_date_candidates(combined_text)
        if len(dates) >= 2:
            return dates[-1]
        primary_window = _select_primary_relative_date_window(relative_windows)
        return primary_window.end.strftime("%Y%m%d") if primary_window else None
    if normalized in TREND_START_DATE_FLAG_NAMES:
        window = _select_secondary_relative_date_window(relative_windows) or _select_primary_relative_date_window(
            relative_windows
        )
        return window.start.strftime("%Y%m%d") if window else None
    if normalized in TREND_END_DATE_FLAG_NAMES:
        window = _select_secondary_relative_date_window(relative_windows) or _select_primary_relative_date_window(
            relative_windows
        )
        return window.end.strftime("%Y%m%d") if window else None
    if normalized in {"target", "target_code", "code", "ticker", "symbol"}:
        symbols = extract_symbol_candidates(combined_text)
        return symbols[0] if symbols else None
    if normalized in {"codes", "symbols", "tickers"}:
        symbols = extract_symbol_candidates(combined_text)
        numeric_symbols = [item for item in symbols if re.fullmatch(r"\d{6}", item)]
        if numeric_symbols:
            return ",".join(numeric_symbols)
    if normalized in {"output", "output_path"}:
        return extract_output_path(constraints)
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


def normalize_generated_entrypoint(
    value: Any,
    *,
    generated_files: list[SkillGeneratedFile],
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized_path = value.replace("\\", "/").strip()
    if not normalized_path:
        return None
    pure_path = PurePosixPath(normalized_path)
    if pure_path.is_absolute() or any(part == ".." for part in pure_path.parts):
        return None
    generated_paths = {
        item.path.replace("\\", "/").strip()
        for item in generated_files
        if item.path.strip()
    }
    normalized_entrypoint = str(pure_path)
    if generated_paths and normalized_entrypoint not in generated_paths:
        return None
    return normalized_entrypoint


def maybe_promote_inline_python_to_generated_script(
    *,
    command: str | None,
    generated_files: list[SkillGeneratedFile],
    entrypoint: str | None,
    cli_args: list[str] | None,
    runtime_target: SkillRuntimeTarget,
    request_text: str,
    python_example_count: int,
) -> tuple[str | None, list[SkillGeneratedFile], str | None, list[str], bool]:
    if generated_files or entrypoint:
        return command, list(generated_files), entrypoint, list(cli_args or []), False

    inline_python = extract_inline_python_command(command)
    if inline_python is None:
        return command, list(generated_files), entrypoint, list(cli_args or []), False

    python_code, command_cli_args = inline_python
    combined_request_text = request_text or ""
    should_promote = (
        python_example_count >= INLINE_PYTHON_SCRIPT_PROMOTION_MIN_EXAMPLES
        or len(python_code) >= INLINE_PYTHON_SCRIPT_PROMOTION_MIN_CHARS
        or bool(SCRIPT_FIRST_REQUEST_PATTERN.search(combined_request_text))
    )
    if not should_promote:
        return command, list(generated_files), entrypoint, list(cli_args or []), False

    normalized_code = python_code.rstrip() + "\n"
    promoted_entrypoint = ".skill_generated/main.py"
    promoted_cli_args = (
        list(cli_args)
        if cli_args
        else [str(item) for item in command_cli_args]
    )
    promoted_generated_files = [
        SkillGeneratedFile(
            path=promoted_entrypoint,
            content=normalized_code,
            description=(
                "Auto-generated from a free-shell inline Python command so the runtime can "
                "write the script first and then execute it."
            ),
        )
    ]
    promoted_command = build_portable_script_command(
        promoted_entrypoint,
        promoted_cli_args,
        runtime_target=runtime_target,
    )
    return (
        promoted_command,
        promoted_generated_files,
        promoted_entrypoint,
        promoted_cli_args,
        True,
    )


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


def _build_preferred_shipped_script_plan(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    shell_hints: dict[str, Any],
    shell_mode: SkillShellMode,
) -> SkillCommandPlan | None:
    query_text = "\n".join(
        [
            request.goal,
            request.user_query,
            " ".join(str(item) for item in request.constraints.values()),
        ]
    )
    documented_scripts = {
        item.replace("\\", "/").strip()
        for item in shell_hints.get("documented_scripts", [])
        if isinstance(item, str) and item.strip()
    }
    previews_by_path = {
        str(item.get("path")): int(item.get("score", 0))
        for item in load_script_previews(loaded_skill, query_text=query_text, limit=8)
        if isinstance(item, dict) and item.get("path")
    }
    candidates: list[dict[str, Any]] = []
    for index, relative_path in enumerate(iter_runnable_scripts(loaded_skill)):
        if documented_scripts and relative_path not in documented_scripts:
            continue
        script_path = (loaded_skill.skill.path / relative_path).resolve()
        cli_args, missing_fields, inferred_flag_count = infer_script_cli_args(
            script_path=script_path,
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
        )
        if missing_fields:
            continue
        preview_score = previews_by_path.get(relative_path, 0)
        relevance_score = _score_relevance(relative_path, query_text) + preview_score
        if inferred_flag_count <= 0 and relevance_score <= 0:
            continue
        candidates.append(
            {
                "entrypoint": relative_path,
                "cli_args": cli_args,
                "missing_fields": missing_fields,
                "inferred_flag_count": inferred_flag_count,
                "relevance_score": relevance_score,
                "index": index,
            }
        )

    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda item: (
            item["inferred_flag_count"],
            item["relevance_score"],
            -item["index"],
        ),
        reverse=True,
    )
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    is_clear_match = (
        best["inferred_flag_count"] > 0
        and (
            second is None
            or best["inferred_flag_count"] > second["inferred_flag_count"]
            or best["relevance_score"] >= second["relevance_score"] + 2
        )
    )
    if not is_clear_match:
        return None

    return SkillCommandPlan(
        skill_name=request.skill_name,
        goal=request.goal,
        user_query=request.user_query,
        runtime_target=request.runtime_target,
        constraints=dict(request.constraints),
        command=build_portable_script_command(
            best["entrypoint"],
            best["cli_args"],
            runtime_target=request.runtime_target,
        ),
        mode="inferred",
        shell_mode=shell_mode,
        rationale=(
            "Locked onto the documented shipped skill script whose CLI could be inferred "
            "from the request."
        ),
        entrypoint=best["entrypoint"],
        cli_args=list(best["cli_args"]),
        missing_fields=[],
        failure_reason=None,
        hints={
            **shell_hints,
            "planner": "locked_shipped_script",
            "shipped_script_locked": True,
            "preferred_shipped_entrypoint": best["entrypoint"],
            "preferred_shipped_relevance_score": best["relevance_score"],
            "preferred_shipped_inferred_flag_count": best["inferred_flag_count"],
        },
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

    async def _maybe_infer_constraints_with_llm(
        self,
        *,
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
        shell_hints: dict[str, Any],
        runtime_target: SkillRuntimeTarget,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self.llm_client is None or not self.llm_client.is_available():
            return dict(request.constraints or {}), None

        allowed_keys = _build_allowed_constraint_keys(loaded_skill, shell_hints)
        if not allowed_keys:
            return dict(request.constraints or {}), None

        _, body = split_skill_markdown(loaded_skill.skill_md)
        skill_md_excerpt = _truncate_text(body, max_chars=MAX_MARKDOWN_EXCERPT_CHARS)
        script_previews = load_script_previews(
            loaded_skill,
            query_text="\n".join(
                [
                    request.goal,
                    request.user_query,
                    " ".join(str(item) for item in request.constraints.values()),
                ]
            ),
            limit=4,
        )
        system_prompt, user_prompt = build_skill_constraint_inference_prompt(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            reference_date=_current_date().isoformat(),
            runtime_target=runtime_target.to_dict(),
            current_constraints=dict(request.constraints or {}),
            allowed_constraint_keys=allowed_keys,
            skill_md_excerpt=skill_md_excerpt,
            script_previews=script_previews,
            file_inventory=[item.to_dict() for item in loaded_skill.file_inventory],
        )
        try:
            payload = await self.llm_client.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=800,
            )
        except Exception:
            return dict(request.constraints or {}), {
                "planner": "constraint_inference",
                "status": "failed",
            }

        sanitized = _sanitize_inferred_constraints(
            payload.get("constraints") if isinstance(payload, dict) else None,
            allowed_keys=set(allowed_keys),
        )
        merged = dict(request.constraints or {})
        applied: dict[str, Any] = {}
        for key, value in sanitized.items():
            existing = merged.get(key)
            if existing is not None and existing != "" and existing != [] and existing != {}:
                continue
            merged[key] = value
            applied[key] = value

        metadata = {
            "planner": "constraint_inference",
            "status": "applied" if applied else "no_change",
            "confidence": (
                str(payload.get("confidence", "")).strip().lower()
                if isinstance(payload, dict)
                else ""
            ),
            "reason": (
                str(payload.get("reason", "")).strip()
                if isinstance(payload, dict)
                else ""
            ),
            "applied": applied,
        }
        return merged, metadata

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
        shell_hints = build_shell_hints(loaded_skill)
        explicit_command = extract_requested_shell_command(constraints)
        constraint_inference_metadata: dict[str, Any] | None = None
        if shell_mode != "free_shell" and explicit_command is None:
            constraints, constraint_inference_metadata = await self._maybe_infer_constraints_with_llm(
                loaded_skill=loaded_skill,
                request=request,
                shell_hints=shell_hints,
                runtime_target=runtime_target,
            )
            shell_mode = resolve_shell_mode(constraints, default=self.default_shell_mode)
        normalized_request = SkillExecutionRequest(
            skill_name=request.skill_name,
            goal=request.goal,
            user_query=request.user_query,
            workspace=request.workspace,
            shell_mode=shell_mode,
            runtime_target=runtime_target,
            constraints=constraints,
        )
        if constraint_inference_metadata:
            shell_hints = {
                **shell_hints,
                "constraint_inference": constraint_inference_metadata,
            }
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
        preferred_shipped_plan = _build_preferred_shipped_script_plan(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
            shell_mode=shell_mode,
        )
        if preferred_shipped_plan is not None:
            return preferred_shipped_plan
        free_shell_plan = await self._plan_free_shell(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
            fallback_plan=conservative_plan,
        )
        if not free_shell_plan.is_manual_required:
            return free_shell_plan
        if (
            not conservative_plan.is_manual_required
            and free_shell_plan.failure_reason in {"llm_not_available", "llm_planning_failed"}
        ):
            return _clone_plan_with_shell_mode(
                conservative_plan,
                shell_mode="free_shell",
            )
        return free_shell_plan

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
        if len(runnable_scripts) > 1:
            preferred_shipped_plan = _build_preferred_shipped_script_plan(
                loaded_skill=loaded_skill,
                request=request,
                shell_hints=shell_hints,
                shell_mode=request.shell_mode,
            )
            if preferred_shipped_plan is not None:
                return preferred_shipped_plan
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
            limit=MAX_FREE_SHELL_EXAMPLE_COUNT,
            max_chars=MAX_FREE_SHELL_EXAMPLE_CHARS,
            max_total_chars=MAX_FREE_SHELL_TOTAL_EXAMPLE_CHARS,
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
            conservative_plan=fallback_plan.to_dict(),
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
        cli_args = normalize_cli_args(payload.get("cli_args")) or []
        bootstrap_commands = normalize_shell_commands(payload.get("bootstrap_commands"))
        bootstrap_reason = str(payload.get("bootstrap_reason", "")).strip()
        generated_entrypoint = normalize_generated_entrypoint(
            payload.get("entrypoint"),
            generated_files=generated_files,
        )
        warnings = [
            str(item)
            for item in (payload.get("warnings") if isinstance(payload.get("warnings"), list) else [])
            if isinstance(item, (str, int, float))
        ]
        command = normalize_shell_command(payload.get("command"))
        command = normalize_generated_command(command, generated_files)
        if command is None and generated_entrypoint:
            command = build_portable_script_command(
                generated_entrypoint,
                cli_args,
                runtime_target=request.runtime_target,
            )
            if raw_mode != "manual_required":
                raw_mode = "generated_script"
        if command is None and generated_files:
            command = default_generated_file_command(
                generated_files,
                runtime_target=request.runtime_target,
                cli_args=cli_args,
            )
            if command and raw_mode != "manual_required":
                raw_mode = "generated_script"
                generated_entrypoint = generated_entrypoint or default_generated_file_entrypoint(
                    generated_files
                )
        if generated_entrypoint is None and generated_files and raw_mode == "generated_script":
            generated_entrypoint = default_generated_file_entrypoint(generated_files)

        (
            command,
            generated_files,
            generated_entrypoint,
            cli_args,
            promoted_inline_python,
        ) = maybe_promote_inline_python_to_generated_script(
            command=command,
            generated_files=generated_files,
            entrypoint=generated_entrypoint,
            cli_args=cli_args,
            runtime_target=request.runtime_target,
            request_text=query_text,
            python_example_count=len(python_examples),
        )
        if promoted_inline_python and raw_mode != "manual_required":
            raw_mode = "generated_script"
            warnings = [
                *warnings,
                "Promoted a free-shell inline Python command into a generated script bundle.",
            ]

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
        hints = {
            **shell_hints,
            "planner": "free_shell",
            "required_tools": required_tools,
            "warnings": warnings,
            "script_previews": script_previews,
            "python_examples": python_examples,
            "promoted_inline_python_to_generated_script": promoted_inline_python,
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
            entrypoint=generated_entrypoint,
            cli_args=cli_args,
            generated_files=generated_files,
            bootstrap_commands=bootstrap_commands,
            bootstrap_reason=bootstrap_reason,
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
            bootstrap_commands=[],
            bootstrap_reason="",
            missing_fields=list(missing_fields),
            failure_reason=failure_reason,
            hints=dict(shell_hints),
        )
