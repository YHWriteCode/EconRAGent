from __future__ import annotations

import ast
import calendar
import json
import os
import re
import shlex
import textwrap
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
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
EXPLICIT_CREDENTIAL_REQUIREMENT_PATTERN = re.compile(
    r"("
    r"required credentials?|"
    r"requires? (?:an? )?(?:api[_ -]?key|access[_ -]?token|credential|secret|password|auth token)|"
    r"provide (?:an? |your )?(?:api[_ -]?key|access[_ -]?token|credential|secret|password|auth token)|"
    r"set (?:an? |your )?(?:api[_ -]?key|access[_ -]?token|credential|secret|password|auth token)"
    r")",
    re.IGNORECASE,
)
ENV_VAR_PATTERN = re.compile(
    r"\$(?:\{(?P<braced>[A-Z][A-Z0-9_]+)\}|(?P<unix>[A-Z][A-Z0-9_]+))|%(?P<win>[A-Z][A-Z0-9_]+)%",
)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
MAX_SCRIPT_PREVIEW_CHARS = 1400
MAX_GENERATED_FILE_COUNT = 5
MAX_GENERATED_FILE_BYTES = 64000
MAX_BOOTSTRAP_COMMAND_COUNT = 4
MAX_FREE_SHELL_EXAMPLE_COUNT = 8
MAX_FREE_SHELL_EXAMPLE_CHARS = 2400
MAX_FREE_SHELL_TOTAL_EXAMPLE_CHARS = 12000
INLINE_PYTHON_SCRIPT_PROMOTION_MIN_EXAMPLES = 3
INLINE_PYTHON_SCRIPT_PROMOTION_MIN_CHARS = 120
PREFERRED_GENERATED_ENTRYPOINT_NAMES = ("main.py", "run.py", "app.py", "script.py")
ENTRYPOINT_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".ps1"}
NON_ENTRY_SCRIPT_DIR_NAMES = {"schemas", "helpers", "validators", "__pycache__"}
TEXT_DOCUMENT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}
NODE_TOOL_NAMES = {"node", "npm", "npx", "yarn", "pnpm"}
NODE_RUNTIME_SUFFIXES = {".js", ".mjs", ".cjs"}
KNOWN_NODE_PACKAGE_NAMES = {"pptxgenjs"}
PIP_INSTALL_OPTION_NAMES_WITH_VALUE = {
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "-i",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--trusted-host",
    "--timeout",
    "--retries",
    "--target",
    "-t",
    "--prefix",
    "--cache-dir",
    "--proxy",
    "--cert",
    "--client-cert",
    "--log",
    "--report",
    "--python-version",
    "--platform",
    "--implementation",
    "--abi",
    "--src",
    "--root",
}
NODE_BOOTSTRAP_PATTERN = re.compile(
    r"\b(?:node|npm|npx|yarn|pnpm)\b",
    re.IGNORECASE,
)
NODE_CONTENT_MARKERS = (
    "pptxgenjs",
    "require(",
    "react-icons",
    "reactdomserver",
    "sharp(",
    "subprocess.run(['node'",
    'subprocess.run(["node"',
    "subprocess.run(['npm'",
    'subprocess.run(["npm"',
    "node ",
    "npm ",
    "npx ",
    "yarn ",
    "pnpm ",
)
DOC_BUNDLE_MAX_HOPS = 2
DOC_BUNDLE_MAX_FOLLOWUPS = 4
DOC_BUNDLE_COMPACT_FOLLOWUP_LIMIT = 2
DOC_BUNDLE_COMPACT_FOLLOWUP_CHARS = 2200
DOC_BUNDLE_MICRO_FOLLOWUP_LIMIT = 1
DOC_BUNDLE_MICRO_FOLLOWUP_CHARS = 1200
CLI_HISTORY_MAX_ATTEMPTS = 3
CLI_HISTORY_STDIO_TAIL_CHARS = 1200
FREE_SHELL_MICRO_FILE_LIMIT = 10
FREE_SHELL_MICRO_SCRIPT_LIMIT = 2
FREE_SHELL_MICRO_SCRIPT_CHARS = 320
FREE_SHELL_MAX_TOKENS = 3200
FREE_SHELL_MICRO_MAX_TOKENS = 2200
FREE_SHELL_REPAIR_OUTPUT_SNIPPET_CHARS = 12000
FREE_SHELL_LARGE_ACTIONABLE_DOC_CHARS = 14000
FREE_SHELL_LARGE_ACTIONABLE_FILE_COUNT = 24
MARKDOWN_RELATIVE_LINK_PATTERN = re.compile(r"\[[^\]]+\]\((?P<target>[^)]+)\)")
INLINE_DOC_REFERENCE_PATTERN = re.compile(
    r"(?P<path>(?:references/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:md|markdown|txt|rst|adoc))",
    re.IGNORECASE,
)
TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")
SMART_QUOTE_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
)
TECHNICAL_MANUAL_FAILURE_REASONS = {
    "environment_not_prepared",
    "llm_not_available_for_free_shell",
    "llm_planning_failed",
    "python_not_supported",
    "prompt_parse_failed",
    "internal_planner_error",
}
USER_ACTIONABLE_MANUAL_FAILURE_REASONS = {
    "missing_credential",
    "ambiguous_target_file",
    "missing_input_path",
    "missing_required_args",
    "manual_command_required",
}
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
COMPANY_TICKER_ALIASES = {
    "tesla": "TSLA",
    "tesla motors": "TSLA",
    "特斯拉": "TSLA",
    "特斯拉汽车": "TSLA",
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
    "format",
    "output_format",
    "topic",
    "title",
    "language",
    "audience",
    "style",
    "theme",
    "slide_count",
    "template",
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
AUTO_FREE_SHELL_FALLBACK_FAILURE_REASONS = {
    "manual_command_required",
    "missing_required_args",
    "environment_not_prepared",
}
AUTO_FREE_SHELL_CREATION_ONLY_FAILURE_REASONS = {
    "missing_input_path",
}
NON_ESCALATABLE_MANUAL_FAILURE_REASONS = {
    "missing_credential",
    "ambiguous_target_file",
}
CREATION_REQUEST_PATTERN = re.compile(
    r"("
    r"create|build|make|generate|draft|compose|produce|"
    r"\u751f\u6210|\u521b\u5efa|\u5236\u4f5c|\u505a(?:\u4e00\u4e2a|\u4e2a)?|\u5199(?:\u4e00\u4e2a|\u4e2a)?"
    r")",
    re.IGNORECASE,
)
ACTIONABLE_SKILL_PATTERN = re.compile(
    r"("
    r"file|files|document|documents|pdf|ppt|pptx|slides?|deck|presentation|"
    r"xlsx|xlsm|xls|csv|tsv|excel|spreadsheet|workbook|worksheet|"
    r"report|reports|table|tables|chart|charts|data|dataset|datasets|"
    r"extract|merge|split|convert|transform|render|rendering|ocr|fill|"
    r"watermark|query|fetch|analy[sz]e|analysis|clean|repair|recalc|"
    r"backtest|quote|stock|price|trend|volatility|"
    r"\u6587\u4ef6|\u6587\u6863|\u8868\u683c|\u5de5\u4f5c\u7c3f|\u7535\u5b50\u8868\u683c|"
    r"\u62a5\u8868|\u62a5\u544a|\u5e7b\u706f\u7247|\u6f14\u793a|\u7b80\u62a5|"
    r"\u63d0\u53d6|\u5408\u5e76|\u62c6\u5206|\u8f6c\u6362|\u6e32\u67d3|\u8865\u5168|"
    r"\u6570\u636e|\u5206\u6790|\u6e05\u6d17|\u56de\u6d4b|\u80a1\u4ef7|\u884c\u60c5|\u8d8b\u52bf|\u6ce2\u52a8"
    r")",
    re.IGNORECASE,
)
FINANCIAL_PIPELINE_REQUEST_PATTERN = re.compile(
    r"("
    r"backtest|"
    r"strategy|"
    r"alpha|"
    r"panel|"
    r"regression|"
    r"factor|"
    r"model|"
    r"\u56de\u6d4b|"
    r"\u7b56\u7565|"
    r"\u56e0\u5b50|"
    r"\u5efa\u6a21|"
    r"\u56de\u5f52|"
    r"\u9762\u677f|"
    r"\u4fe1\u53f7"
    r")",
    re.IGNORECASE,
)
FINANCIAL_TREND_REQUEST_PATTERN = re.compile(
    r"("
    r"trend|"
    r"volatility|"
    r"price\s+movement|"
    r"candlestick|"
    r"ohlc|"
    r"\u8d8b\u52bf|"
    r"\u8d70\u52bf|"
    r"\u6ce2\u52a8|"
    r"\u6da8\u8dcc|"
    r"\u6da8\u5e45|"
    r"\u8dcc\u5e45|"
    r"\u4e0a\u6da8|"
    r"\u4e0b\u8dcc|"
    r"k\u7ebf"
    r")",
    re.IGNORECASE,
)
FINANCIAL_FETCH_REQUEST_PATTERN = re.compile(
    r"("
    r"quote|"
    r"price|"
    r"market\s+data|"
    r"history|"
    r"historical|"
    r"fetch|"
    r"pull|"
    r"get|"
    r"current|"
    r"today|"
    r"yesterday|"
    r"\u80a1\u4ef7|"
    r"\u884c\u60c5|"
    r"\u5f00\u76d8\u4ef7|"
    r"\u6536\u76d8\u4ef7|"
    r"\u6700\u9ad8\u4ef7|"
    r"\u6700\u4f4e\u4ef7|"
    r"\u6210\u4ea4\u91cf|"
    r"\u6210\u4ea4\u989d|"
    r"\u5386\u53f2\u6570\u636e|"
    r"\u62c9\u53d6|"
    r"\u83b7\u53d6|"
    r"\u67e5(?:\u4e00\u4e0b|\u8be2|\u627e)?|"
    r"\u4eca\u5929|"
    r"\u6628\u5929|"
    r"\u5f53\u524d"
    r")",
    re.IGNORECASE,
)
RELATIVE_YESTERDAY_PATTERN = re.compile(
    r"("
    r"\u6628\u5929|"
    r"\u6628\u65e5|"
    r"yesterday"
    r")",
    re.IGNORECASE,
)
RELATIVE_TODAY_PATTERN = re.compile(
    r"("
    r"\u4eca\u5929|"
    r"\u4eca\u65e5|"
    r"\u73b0\u5728|"
    r"\u5f53\u524d|"
    r"\u6700\u65b0|"
    r"\u5b9e\u65f6|"
    r"today|"
    r"current|"
    r"now|"
    r"latest|"
    r"real.?time"
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


def _extract_json_object_text(payload: str) -> str:
    normalized = str(payload or "").strip()
    if not normalized:
        raise ValueError("LLM did not return a JSON object.")
    fence_match = CODE_FENCE_PATTERN.search(normalized)
    if fence_match:
        normalized = str(fence_match.group("code") or "").strip()
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM did not return a JSON object: {payload}")
    return normalized[start : end + 1]


def _normalize_json_like_text(payload: str) -> str:
    normalized = str(payload or "").translate(SMART_QUOTE_TRANSLATION)
    return TRAILING_COMMA_PATTERN.sub(r"\1", normalized)


def _parse_json_like_object(payload: str) -> dict[str, Any]:
    object_text = _extract_json_object_text(payload)
    candidates: list[str] = []
    for item in (object_text, _normalize_json_like_text(object_text)):
        if item and item not in candidates:
            candidates.append(item)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception as exc:
            last_error = exc
        try:
            value = ast.literal_eval(candidate)
            if isinstance(value, dict):
                return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError(f"LLM did not return a JSON object: {payload}")


async def _complete_json_via_text_first(
    llm_client: SkillPlannerLLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    complete_text = getattr(llm_client, "complete_text", None)
    if complete_text is None:
        raise RuntimeError("LLM client does not support text-first planning fallback.")

    raw_text = await complete_text(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        return _parse_json_like_object(raw_text)
    except Exception as parse_exc:
        repair_system_prompt = (
            "You repair malformed or oversized JSON skill plans returned by another model. "
            "Return exactly one valid JSON object and nothing else. "
            "Preserve the original plan intent, but you may rewrite the plan into a more compact equivalent form "
            "when the original output was truncated, oversized, or invalid. "
            "If generated_files appear in the malformed output, keep them minimal: prefer one small executable "
            "entrypoint file, do not inline assets/base64/large article text/templates, and keep each generated file "
            "well under a large-script threshold. "
            "If a Node-only package appears, do not import it from Python; use bootstrap_commands or a Node entrypoint instead. "
            "If you cannot safely reconstruct a runnable plan, return a valid manual_required JSON object."
        )
        repair_user_prompt = (
            "The following model output should have been a compact JSON object but failed to parse.\n\n"
            "Return the same planner schema as a compact valid JSON object:\n"
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
            f"Parse error:\n{parse_exc}\n\n"
            "Repair guidance:\n"
            "- If the malformed output was truncated inside generated_files.content, replace that content with a much shorter executable equivalent.\n"
            "- Prefer shipped entrypoints, bootstrap_commands, and concise generated_files over long embedded scripts.\n"
            "- Never return markdown fences.\n\n"
            f"Original planner system prompt:\n{system_prompt}\n\n"
            f"Malformed output:\n{_truncate_text(raw_text, max_chars=FREE_SHELL_REPAIR_OUTPUT_SNIPPET_CHARS)}\n"
        )
        repaired_text = await complete_text(
            system_prompt=repair_system_prompt,
            user_prompt=repair_user_prompt,
            temperature=0.0,
            max_tokens=max(800, min(max_tokens, 2400)),
        )
        try:
            return _parse_json_like_object(repaired_text)
        except Exception as repair_exc:
            raise ValueError(
                f"Text-first JSON parsing failed. Initial error: {parse_exc}. "
                f"Repair error: {repair_exc}."
            ) from repair_exc


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


def _format_yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


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


def _extract_code_like_value(value: Any) -> str | None:
    normalized = _normalize_code_like_value(value)
    if normalized is not None:
        return normalized
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    for alias, ticker in COMPANY_TICKER_ALIASES.items():
        if alias.casefold() in lowered:
            return ticker
    candidates = extract_symbol_candidates(text)
    if candidates:
        return candidates[0]
    return None


def _normalize_multi_code_like_value(value: Any) -> str | None:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        parts = [item.strip() for item in str(value or "").split(",") if item.strip()]
    normalized_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = _extract_code_like_value(part)
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
    return sorted(allowed)


def _sanitize_inferred_constraint_value(key: str, value: Any) -> Any:
    normalized_key = str(key or "").strip().lower()
    if not normalized_key:
        return None
    if normalized_key in DATE_LIKE_CONSTRAINT_NAMES:
        candidate = str(value or "").strip()
        return candidate if re.fullmatch(r"20\d{6}", candidate) else None
    if normalized_key in {"code", "target", "target_code", "ticker", "symbol"}:
        return _extract_code_like_value(value)
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
        "topic",
        "title",
        "language",
        "audience",
        "style",
        "theme",
        "template",
        "mode",
        "dep",
        "indep",
        "notes",
        "note",
        "description",
        "operation",
    }:
        return _normalize_simple_string(value)
    if normalized_key == "slide_count":
        if isinstance(value, (int, float)) and int(value) > 0:
            return str(int(value))
        candidate = str(value or "").strip()
        return candidate if candidate.isdigit() and int(candidate) > 0 else None
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


def _join_request_context_text(*parts: Any) -> str:
    normalized_parts: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            normalized_parts.extend(str(item) for item in part.values())
            continue
        if isinstance(part, (list, tuple, set)):
            normalized_parts.extend(str(item) for item in part)
            continue
        if part not in (None, ""):
            normalized_parts.append(str(part))
    return "\n".join(item for item in normalized_parts if item.strip())


def _infer_financial_research_request_kind(
    *,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> str | None:
    query_text = _join_request_context_text(goal, user_query, constraints)
    if not query_text.strip():
        return None
    if FINANCIAL_PIPELINE_REQUEST_PATTERN.search(query_text):
        return "pipeline"
    if FINANCIAL_TREND_REQUEST_PATTERN.search(query_text):
        return "trend"
    if FINANCIAL_FETCH_REQUEST_PATTERN.search(query_text):
        return "fetch"
    return None


def _infer_financial_default_dates(
    *,
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> dict[str, str]:
    query_text = _join_request_context_text(goal, user_query, constraints)
    request_kind = _infer_financial_research_request_kind(
        goal=goal,
        user_query=user_query,
        constraints=constraints,
    )
    if request_kind not in {"fetch", "trend"}:
        return {}

    reference_date = _current_date()
    if RELATIVE_YESTERDAY_PATTERN.search(query_text):
        target_date = reference_date - timedelta(days=1)
        formatted = _format_yyyymmdd(target_date)
        defaults = {
            "start": formatted,
            "end": formatted,
        }
        if request_kind == "trend":
            defaults["trend_start"] = formatted
            defaults["trend_end"] = formatted
        return defaults

    if request_kind == "fetch":
        start_date = reference_date - timedelta(days=7)
        end_date = reference_date
        return {
            "start": _format_yyyymmdd(start_date),
            "end": _format_yyyymmdd(end_date),
        }

    start_date = reference_date - timedelta(days=90)
    trend_start_date = reference_date - timedelta(days=30)
    return {
        "start": _format_yyyymmdd(start_date),
        "end": _format_yyyymmdd(reference_date),
        "trend_start": _format_yyyymmdd(trend_start_date),
        "trend_end": _format_yyyymmdd(reference_date),
    }


def _financial_research_entrypoint_kind(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/").strip().lower()
    if normalized.endswith("scripts/fetch_market_data.py"):
        return "fetch"
    if normalized.endswith("scripts/analyze_stock_trend.py"):
        return "trend"
    if normalized.endswith("scripts/fetch_model_backtest.py"):
        return "pipeline"
    if normalized.endswith("scripts/prepare_panel_data.py"):
        return "panel"
    if normalized.endswith("scripts/run_panel_model.py"):
        return "model"
    if normalized.endswith("scripts/run_backtest.py"):
        return "backtest"
    return None


def _score_financial_research_candidate(
    *,
    relative_path: str,
    request_kind: str | None,
) -> tuple[bool, int]:
    candidate_kind = _financial_research_entrypoint_kind(relative_path)
    if request_kind == "fetch":
        if candidate_kind == "fetch":
            return False, 8
        if candidate_kind in {"trend", "pipeline", "panel", "model", "backtest"}:
            return True, 0
    if request_kind == "trend":
        if candidate_kind == "trend":
            return False, 8
        if candidate_kind in {"fetch", "pipeline", "panel", "model", "backtest"}:
            return True, 0
    if request_kind == "pipeline":
        if candidate_kind == "pipeline":
            return False, 8
        if candidate_kind in {"panel", "model", "backtest"}:
            return False, 3
        if candidate_kind in {"fetch", "trend"}:
            return True, 0
    return False, 0


def _script_output_flag_expects_directory(script_path: Path) -> bool:
    default_output = str(extract_cli_flag_defaults(script_path).get("--output") or "").strip()
    if not default_output:
        return False
    normalized = default_output.replace("\\", "/").rstrip("/")
    if not normalized:
        return False
    return PurePosixPath(normalized).suffix == ""


def _normalize_output_argument_for_script(
    *,
    script_path: Path,
    output_path: str,
) -> str:
    normalized_output = str(output_path or "").strip()
    if not normalized_output or not _script_output_flag_expects_directory(script_path):
        return normalized_output
    if normalized_output.endswith(("/", "\\")):
        return normalized_output.rstrip("/\\")

    default_output = str(extract_cli_flag_defaults(script_path).get("--output") or "output").strip()
    path_type = PureWindowsPath if re.match(r"^[A-Za-z]:[\\/]", normalized_output) or "\\" in normalized_output else PurePosixPath
    candidate_path = path_type(normalized_output)
    if candidate_path.suffix:
        parent = str(candidate_path.parent)
        if parent not in {"", "."}:
            return parent
        return default_output
    return normalized_output


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


def extract_documented_entrypoint_paths(markdown: str) -> list[str]:
    documented: list[str] = []
    seen: set[str] = set()
    for path in extract_documented_script_paths(markdown):
        if path in seen:
            continue
        documented.append(path)
        seen.add(path)
    for command in extract_shell_examples(markdown):
        for match in re.finditer(r"(?P<path>scripts/[A-Za-z0-9_./-]+\.(?:py|sh|bash|ps1))", command):
            path = str(match.group("path") or "").replace("\\", "/").strip()
            if not path or path in seen:
                continue
            documented.append(path)
            seen.add(path)
    return documented


def _is_candidate_entry_script(
    relative_path: str,
    *,
    documented_entrypoints: set[str] | None = None,
) -> bool:
    normalized = str(relative_path or "").replace("\\", "/").strip()
    if not normalized:
        return False
    pure_path = PurePosixPath(normalized)
    suffix = pure_path.suffix.lower()
    if suffix not in ENTRYPOINT_SCRIPT_SUFFIXES:
        return False
    if pure_path.name.startswith("__init__."):
        return False
    normalized_documented = documented_entrypoints or set()
    lower_parts = [part.lower() for part in pure_path.parts]
    if any(part in NON_ENTRY_SCRIPT_DIR_NAMES for part in lower_parts):
        return normalized in normalized_documented
    return True


def iter_runnable_scripts(
    loaded_skill: LoadedSkill,
    *,
    documented_entrypoints: set[str] | None = None,
) -> list[str]:
    runnable: list[str] = []
    for item in loaded_skill.file_inventory:
        if item.kind != "script":
            continue
        relative_path = item.path.replace("\\", "/")
        if _is_candidate_entry_script(
            relative_path,
            documented_entrypoints=documented_entrypoints,
        ):
            runnable.append(relative_path)
    return runnable


def _read_skill_text_file(
    loaded_skill: LoadedSkill,
    relative_path: str,
) -> str | None:
    normalized = str(relative_path or "").replace("\\", "/").strip()
    if not normalized:
        return None
    absolute_path = (loaded_skill.skill.path / normalized).resolve()
    try:
        skill_root = loaded_skill.skill.path.resolve()
    except OSError:
        return None
    if absolute_path != skill_root and skill_root not in absolute_path.parents:
        return None
    if not absolute_path.is_file():
        return None
    try:
        return absolute_path.read_text(encoding="utf-8")
    except Exception:
        return None


def _resolve_relative_skill_doc_reference(
    *,
    loaded_skill: LoadedSkill,
    source_path: str,
    target: str,
) -> str | None:
    normalized_target = str(target or "").strip()
    if not normalized_target:
        return None
    normalized_target = normalized_target.split("#", 1)[0].split("?", 1)[0].strip()
    if not normalized_target or "://" in normalized_target or normalized_target.startswith(("mailto:", "#")):
        return None
    source_relative = str(source_path or "SKILL.md").replace("\\", "/").strip() or "SKILL.md"
    source_absolute = (loaded_skill.skill.path / source_relative).resolve()
    candidate = (source_absolute.parent / normalized_target).resolve()
    skill_root = loaded_skill.skill.path.resolve()
    if candidate != skill_root and skill_root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    suffix = candidate.suffix.lower()
    if suffix not in TEXT_DOCUMENT_SUFFIXES:
        return None
    return str(candidate.relative_to(skill_root)).replace("\\", "/")


def _iter_skill_doc_reference_candidates(
    *,
    loaded_skill: LoadedSkill,
    source_path: str,
    content: str,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in MARKDOWN_RELATIVE_LINK_PATTERN.finditer(content):
        relative_path = _resolve_relative_skill_doc_reference(
            loaded_skill=loaded_skill,
            source_path=source_path,
            target=str(match.group("target") or ""),
        )
        if not relative_path or relative_path in seen:
            continue
        context = content[max(0, match.start() - 120) : min(len(content), match.end() + 120)]
        references.append(
            {
                "path": relative_path,
                "source_path": source_path,
                "context": context,
            }
        )
        seen.add(relative_path)
    for match in INLINE_DOC_REFERENCE_PATTERN.finditer(content):
        relative_path = _resolve_relative_skill_doc_reference(
            loaded_skill=loaded_skill,
            source_path=source_path,
            target=str(match.group("path") or ""),
        )
        if not relative_path or relative_path in seen:
            continue
        context = content[max(0, match.start() - 120) : min(len(content), match.end() + 120)]
        references.append(
            {
                "path": relative_path,
                "source_path": source_path,
                "context": context,
            }
        )
        seen.add(relative_path)
    return references


def _is_edit_or_template_request(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
) -> bool:
    constraints = dict(request.constraints or {})
    if extract_input_paths(constraints):
        return True
    if str(constraints.get("template", "") or "").strip():
        return True
    if loaded_skill.skill.name.strip().lower() != "pptx":
        return False
    request_text = _join_request_context_text(
        request.goal,
        request.user_query,
        request.constraints,
    ).lower()
    return bool(
        re.search(
            r"(edit|update|modify|template|revise|refine|套用模板|模板|编辑|修改|更新)",
            request_text,
            re.IGNORECASE,
        )
    )


def _score_skill_doc_candidate(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    relative_path: str,
    content: str,
    context: str,
) -> int:
    request_text = _join_request_context_text(
        request.goal,
        request.user_query,
        request.constraints,
    )
    corpus = "\n".join([relative_path, context, content[:4000]])
    score = _score_relevance(corpus, request_text)
    normalized_path = relative_path.lower()
    if loaded_skill.skill.name.strip().lower() == "pptx":
        if _is_creation_or_generation_request(loaded_skill=loaded_skill, request=request):
            if normalized_path.endswith("pptxgenjs.md"):
                score += 16
            if normalized_path.endswith("editing.md"):
                score += 2
        if _is_edit_or_template_request(loaded_skill=loaded_skill, request=request):
            if normalized_path.endswith("editing.md"):
                score += 16
            if normalized_path.endswith("pptxgenjs.md"):
                score += 1
    return score


def build_skill_doc_bundle(
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    *,
    max_hops: int = DOC_BUNDLE_MAX_HOPS,
    max_followups: int = DOC_BUNDLE_MAX_FOLLOWUPS,
) -> list[dict[str, Any]]:
    root_entry = {
        "path": "SKILL.md",
        "hop": 0,
        "source_path": None,
        "score": 10_000,
        "content": loaded_skill.skill_md,
        "reason": "root_skill_doc",
    }
    if max_hops <= 0 or max_followups <= 0:
        return [root_entry]

    bundle: list[dict[str, Any]] = [root_entry]
    visited: set[str] = {"SKILL.md"}
    frontier: list[tuple[str, str, int]] = [("SKILL.md", loaded_skill.skill_md, 0)]
    while frontier and len(bundle) - 1 < max_followups:
        discovered: list[dict[str, Any]] = []
        for source_path, content, hop in frontier:
            if hop >= max_hops:
                continue
            for candidate in _iter_skill_doc_reference_candidates(
                loaded_skill=loaded_skill,
                source_path=source_path,
                content=content,
            ):
                relative_path = str(candidate.get("path", "")).strip()
                if not relative_path or relative_path in visited:
                    continue
                candidate_content = _read_skill_text_file(loaded_skill, relative_path)
                if not candidate_content:
                    continue
                discovered.append(
                    {
                        "path": relative_path,
                        "hop": hop + 1,
                        "source_path": source_path,
                        "score": _score_skill_doc_candidate(
                            loaded_skill=loaded_skill,
                            request=request,
                            relative_path=relative_path,
                            content=candidate_content,
                            context=str(candidate.get("context", "")),
                        ),
                        "content": candidate_content,
                        "reason": "progressive_followup",
                    }
                )
        if not discovered:
            break
        discovered.sort(
            key=lambda item: (
                int(item.get("score", 0)),
                -int(item.get("hop", 0)),
                -len(str(item.get("content", ""))),
                str(item.get("path", "")),
            ),
            reverse=True,
        )
        next_frontier: list[tuple[str, str, int]] = []
        for candidate in discovered:
            relative_path = str(candidate.get("path", "")).strip()
            if not relative_path or relative_path in visited:
                continue
            visited.add(relative_path)
            bundle.append(candidate)
            next_frontier.append(
                (
                    relative_path,
                    str(candidate.get("content", "")),
                    int(candidate.get("hop", 0)),
                )
            )
            if len(bundle) - 1 >= max_followups:
                break
        frontier = next_frontier
    return bundle


def compact_skill_doc_bundle(
    doc_bundle: list[dict[str, Any]],
    *,
    followup_limit: int = DOC_BUNDLE_COMPACT_FOLLOWUP_LIMIT,
    followup_chars: int = DOC_BUNDLE_COMPACT_FOLLOWUP_CHARS,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    followup_count = 0
    for item in doc_bundle:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        compacted_item = dict(item)
        if path != "SKILL.md":
            if followup_count >= max(0, followup_limit):
                continue
            compacted_item["content"] = _truncate_text(
                str(item.get("content", "")),
                max_chars=followup_chars,
            )
            followup_count += 1
        compacted.append(compacted_item)
    return compacted


def summarize_cli_history(
    history: list[dict[str, Any]] | None,
    *,
    limit: int = CLI_HISTORY_MAX_ATTEMPTS,
) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    tail = history[-max(1, limit) :] if history else []
    for item in tail:
        if not isinstance(item, dict):
            continue
        summarized.append(
            {
                "attempt_index": item.get("attempt_index"),
                "stage": item.get("stage"),
                "command": item.get("command"),
                "plan_mode": item.get("plan_mode"),
                "entrypoint": item.get("entrypoint"),
                "failure_reason": item.get("failure_reason"),
                "preflight_failure_reason": item.get("preflight_failure_reason"),
                "exit_code": item.get("exit_code"),
                "stdout_tail": _truncate_text(
                    str(item.get("stdout_tail", "")),
                    max_chars=CLI_HISTORY_STDIO_TAIL_CHARS,
                ),
                "stderr_tail": _truncate_text(
                    str(item.get("stderr_tail", "")),
                    max_chars=CLI_HISTORY_STDIO_TAIL_CHARS,
                ),
            }
        )
    return summarized


def classify_manual_required_kind(failure_reason: str | None) -> str:
    normalized = str(failure_reason or "").strip().lower()
    if normalized in TECHNICAL_MANUAL_FAILURE_REASONS:
        return "technical_blocked"
    if normalized in USER_ACTIONABLE_MANUAL_FAILURE_REASONS:
        return "user_actionable"
    return "user_actionable"


def summarize_planner_error(
    *messages: str | None,
    max_chars: int = 220,
) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for message in messages:
        normalized = str(message or "").strip()
        if not normalized:
            continue
        normalized = re.sub(r"\s+", " ", normalized)
        if normalized in seen:
            continue
        seen.add(normalized)
        parts.append(normalized)
    if not parts:
        return None
    summary = "; ".join(parts)
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1].rstrip() + "…"


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
    elif suffix in NODE_RUNTIME_SUFFIXES:
        argv = ["node", normalized_entrypoint]
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
    documented_scripts = extract_documented_entrypoint_paths(loaded_skill.skill_md)
    documented_entrypoints = {
        item.replace("\\", "/").strip()
        for item in documented_scripts
        if str(item).strip()
    }
    runnable_scripts = iter_runnable_scripts(
        loaded_skill,
        documented_entrypoints=documented_entrypoints,
    )
    auto_generated_commands = [
        build_portable_script_command(relative_path) for relative_path in runnable_scripts[:3]
    ]
    required_credentials = extract_required_credentials(
        markdown=loaded_skill.skill_md,
        example_commands=example_commands,
    )
    runnable_script_summaries = [
        {
            "path": relative_path,
            "documented": relative_path in documented_entrypoints,
        }
        for relative_path in runnable_scripts[:8]
    ]
    return {
        "execution_mode": "shell",
        "example_commands": example_commands[:5],
        "auto_generated_commands": auto_generated_commands,
        "runnable_scripts": runnable_scripts,
        "documented_scripts": documented_scripts,
        "runnable_script_summaries": runnable_script_summaries,
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
    if EXPLICIT_CREDENTIAL_REQUIREMENT_PATTERN.search(markdown):
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
    if "alphabetic_ticker_supported = true" in lowered:
        return False
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
        if flag == "--output":
            value = _normalize_output_argument_for_script(
                script_path=script_path,
                output_path=value,
            )
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
        inferred.extend(
            [
                "--output",
                _normalize_output_argument_for_script(
                    script_path=script_path,
                    output_path=output_path,
                ),
            ]
        )
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
    financial_default_dates = _infer_financial_default_dates(
        goal=goal,
        user_query=user_query,
        constraints=constraints,
    )
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
        if normalized in CODE_LIKE_FLAG_NAMES:
            extracted_code = _extract_code_like_value(value)
            if extracted_code:
                return extracted_code
        if normalized in {"codes", "symbols", "tickers"}:
            extracted_codes = _normalize_multi_code_like_value(value)
            if extracted_codes:
                return extracted_codes
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
        if primary_window:
            return primary_window.start.strftime("%Y%m%d")
        return financial_default_dates.get("start")
    if normalized in END_DATE_FLAG_NAMES:
        dates = extract_date_candidates(combined_text)
        if len(dates) >= 2:
            return dates[-1]
        primary_window = _select_primary_relative_date_window(relative_windows)
        if primary_window:
            return primary_window.end.strftime("%Y%m%d")
        return financial_default_dates.get("end")
    if normalized in TREND_START_DATE_FLAG_NAMES:
        window = _select_secondary_relative_date_window(relative_windows) or _select_primary_relative_date_window(
            relative_windows
        )
        if window:
            return window.start.strftime("%Y%m%d")
        return financial_default_dates.get("trend_start")
    if normalized in TREND_END_DATE_FLAG_NAMES:
        window = _select_secondary_relative_date_window(relative_windows) or _select_primary_relative_date_window(
            relative_windows
        )
        if window:
            return window.end.strftime("%Y%m%d")
        return financial_default_dates.get("trend_end")
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


def _command_uses_python_runtime(command: str | None) -> bool:
    normalized = str(command or "").strip().lower()
    return bool(
        normalized.startswith("python ")
        or normalized.startswith("python3 ")
        or normalized.startswith("py ")
    )


def _split_shell_argv(command: str) -> list[str] | None:
    normalized = str(command or "").strip()
    if not normalized:
        return None
    for posix_mode in (True, False):
        try:
            argv = shlex.split(normalized, posix=posix_mode)
        except ValueError:
            continue
        if argv:
            return [str(item) for item in argv]
    return None


def _normalize_bootstrap_package_name(token: str) -> str:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return ""
    normalized = normalized.lstrip()
    for delimiter in ("[", "<", ">", "=", "!", "~"):
        if delimiter in normalized:
            normalized = normalized.split(delimiter, 1)[0].strip()
    return normalized


def _extract_known_node_packages_from_pip_command(
    command: str,
) -> tuple[list[str], list[str], bool] | None:
    argv = _split_shell_argv(command)
    if not argv:
        return None

    install_index: int | None = None
    command_name = Path(argv[0]).name.lower()
    if (
        len(argv) >= 4
        and _is_python_command_name(argv[0])
        and argv[1:4] == ["-m", "pip", "install"]
    ):
        install_index = 3
    elif command_name in {"pip", "pip3", "pip.exe", "pip3.exe"} and len(argv) >= 2 and argv[1] == "install":
        install_index = 1
    if install_index is None:
        return None

    rebuilt_tokens = argv[: install_index + 1]
    node_packages: list[str] = []
    retained_python_packages = False
    index = install_index + 1
    while index < len(argv):
        token = str(argv[index])
        if token in PIP_INSTALL_OPTION_NAMES_WITH_VALUE:
            rebuilt_tokens.append(token)
            if index + 1 < len(argv):
                rebuilt_tokens.append(str(argv[index + 1]))
            index += 2
            continue
        if token.startswith("-"):
            rebuilt_tokens.append(token)
            index += 1
            continue
        normalized_package = _normalize_bootstrap_package_name(token)
        if normalized_package in KNOWN_NODE_PACKAGE_NAMES:
            node_packages.append(token)
        else:
            rebuilt_tokens.append(token)
            retained_python_packages = True
        index += 1

    if not node_packages:
        return None
    return rebuilt_tokens, node_packages, retained_python_packages


def _rewrite_known_node_package_bootstrap_commands(
    bootstrap_commands: list[str],
) -> tuple[list[str], list[str]]:
    rewritten_commands: list[str] = []
    rewritten_packages: list[str] = []
    for command in bootstrap_commands:
        extracted = _extract_known_node_packages_from_pip_command(command)
        if extracted is None:
            rewritten_commands.append(command)
            continue
        rebuilt_tokens, node_packages, retained_python_packages = extracted
        if retained_python_packages:
            rewritten_commands.append(shlex.join(rebuilt_tokens))
        rewritten_commands.append(
            "npm install -g " + " ".join(shlex.quote(str(item)) for item in node_packages)
        )
        rewritten_packages.extend(
            _normalize_bootstrap_package_name(item) for item in node_packages
        )
    return rewritten_commands, [item for item in rewritten_packages if item]


def _plan_requires_node_runtime(
    *,
    command: str | None,
    entrypoint: str | None,
    generated_files: list[SkillGeneratedFile],
) -> bool:
    normalized_entrypoint = str(entrypoint or "").strip().lower()
    if any(normalized_entrypoint.endswith(suffix) for suffix in NODE_RUNTIME_SUFFIXES):
        return True
    if NODE_BOOTSTRAP_PATTERN.search(str(command or "")):
        return True
    for item in generated_files:
        content = str(item.content or "").lower()
        if any(marker in content for marker in NODE_CONTENT_MARKERS):
            return True
    return False


def normalize_free_shell_runtime_requirements(
    *,
    runtime_target: SkillRuntimeTarget,
    command: str | None,
    entrypoint: str | None,
    generated_files: list[SkillGeneratedFile],
    bootstrap_commands: list[str],
    required_tools: list[str],
    warnings: list[str],
    bootstrap_reason: str,
) -> tuple[list[str], list[str], list[str], str]:
    normalized_bootstrap_commands = list(bootstrap_commands)
    normalized_warnings = list(warnings)
    normalized_bootstrap_reason = str(bootstrap_reason or "").strip()

    rewritten_bootstrap_commands, rewritten_node_packages = (
        _rewrite_known_node_package_bootstrap_commands(normalized_bootstrap_commands)
    )
    if rewritten_node_packages:
        normalized_bootstrap_commands = rewritten_bootstrap_commands
        normalized_warnings.append(
            "Rewrote known Node package bootstrap from pip to npm for: "
            + ", ".join(sorted(set(rewritten_node_packages)))
            + "."
        )

    normalized_required_tools: list[str] = []
    seen_tools: set[str] = set()
    inferred_required_tools = list(required_tools)

    normalized_entrypoint = str(entrypoint or "").strip().lower()
    uses_python_runtime = (
        _command_uses_python_runtime(command)
        or normalized_entrypoint.endswith(".py")
        or any(item.path.lower().endswith(".py") for item in generated_files)
    )
    if uses_python_runtime:
        inferred_required_tools.append("python")
    if any(re.search(r"\b(?:pip|python\s+-m\s+pip)\b", item, re.IGNORECASE) for item in normalized_bootstrap_commands):
        inferred_required_tools.extend(["python", "pip"])
    if any(NODE_BOOTSTRAP_PATTERN.search(item) for item in normalized_bootstrap_commands):
        inferred_required_tools.extend(["node", "npm"])
    if _plan_requires_node_runtime(
        command=command,
        entrypoint=entrypoint,
        generated_files=generated_files,
    ):
        inferred_required_tools.extend(["node"])
        if any(NODE_BOOTSTRAP_PATTERN.search(item) for item in normalized_bootstrap_commands):
            inferred_required_tools.extend(["npm"])

    for raw_tool in inferred_required_tools:
        normalized = str(raw_tool or "").strip().lower()
        if not normalized or normalized in seen_tools:
            continue
        seen_tools.add(normalized)
        normalized_required_tools.append(normalized)

    if (
        runtime_target.supports_python
        and uses_python_runtime
        and not _plan_requires_node_runtime(
            command=command,
            entrypoint=entrypoint,
            generated_files=generated_files,
        )
    ):
        filtered_bootstrap_commands = [
            item
            for item in normalized_bootstrap_commands
            if not NODE_BOOTSTRAP_PATTERN.search(item)
        ]
        filtered_required_tools = [
            item
            for item in normalized_required_tools
            if item not in NODE_TOOL_NAMES
        ]
        if (
            len(filtered_bootstrap_commands) != len(normalized_bootstrap_commands)
            or len(filtered_required_tools) != len(normalized_required_tools)
        ):
            normalized_warnings.append(
                "Dropped Node/npm bootstrap steps from a Python-based generated plan because the plan did not actually depend on a Node runtime."
            )
            normalized_bootstrap_commands = filtered_bootstrap_commands
            normalized_required_tools = filtered_required_tools
            if not normalized_bootstrap_commands:
                normalized_bootstrap_reason = ""

    return (
        normalized_bootstrap_commands,
        normalized_required_tools,
        normalized_warnings,
        normalized_bootstrap_reason,
    )


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
    candidate_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    documented_entrypoints = {
        item.replace("\\", "/").strip()
        for item in extract_documented_entrypoint_paths(loaded_skill.skill_md)
        if str(item).strip()
    }
    selected_paths = candidate_paths or iter_runnable_scripts(
        loaded_skill,
        documented_entrypoints=documented_entrypoints,
    )
    for relative_path in selected_paths:
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


def _compact_file_inventory_for_free_shell(
    file_inventory: list[dict[str, Any]],
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    candidates = [
        dict(item)
        for item in file_inventory
        if isinstance(item, dict)
    ]
    prioritized = sorted(
        candidates,
        key=lambda item: (
            0
            if str(item.get("kind", "")).strip() in {"skill_doc", "script", "markdown", "reference"}
            else 1,
            0
            if str(item.get("path", "")).replace("\\", "/") == "SKILL.md"
            else 1,
            str(item.get("path", "")),
        ),
    )
    return prioritized[: max(1, limit)]


def _filter_file_inventory_for_free_shell(
    file_inventory: list[dict[str, Any]],
    *,
    doc_bundle: list[dict[str, Any]],
    candidate_script_paths: list[str],
    limit: int = 40,
) -> list[dict[str, Any]]:
    doc_paths = {
        str(item.get("path", "")).replace("\\", "/").strip()
        for item in doc_bundle
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    }
    candidate_paths = {
        str(item).replace("\\", "/").strip()
        for item in candidate_script_paths
        if str(item).strip()
    }
    filtered: list[dict[str, Any]] = []
    for item in file_inventory:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).replace("\\", "/").strip()
        kind = str(item.get("kind", "")).strip().lower()
        if not path:
            continue
        if path in doc_paths:
            filtered.append(dict(item))
            continue
        if kind == "script" and path in candidate_paths:
            filtered.append(dict(item))
            continue
        if kind in {"skill_doc", "markdown", "reference"}:
            filtered.append(dict(item))
    prioritized = sorted(
        filtered,
        key=lambda item: (
            0 if str(item.get("path", "")).replace("\\", "/") == "SKILL.md" else 1,
            0
            if str(item.get("path", "")).replace("\\", "/") in doc_paths
            else 1,
            0
            if str(item.get("path", "")).replace("\\", "/") in candidate_paths
            else 1,
            str(item.get("path", "")),
        ),
    )
    return prioritized[: max(1, limit)]


def _compact_script_previews_for_free_shell(
    script_previews: list[dict[str, Any]],
    *,
    limit: int = 2,
    preview_chars: int = 500,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in script_previews[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "path": str(item.get("path", "")).strip(),
                "score": item.get("score"),
                "preview": _truncate_text(
                    str(item.get("preview", "")),
                    max_chars=preview_chars,
                ),
            }
        )
    return compacted


def _micro_file_inventory_for_free_shell(
    file_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _compact_file_inventory_for_free_shell(
        file_inventory,
        limit=FREE_SHELL_MICRO_FILE_LIMIT,
    )


def _micro_script_previews_for_free_shell(
    script_previews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _compact_script_previews_for_free_shell(
        script_previews,
        limit=FREE_SHELL_MICRO_SCRIPT_LIMIT,
        preview_chars=FREE_SHELL_MICRO_SCRIPT_CHARS,
    )


def _micro_doc_bundle_for_free_shell(
    doc_bundle: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return compact_skill_doc_bundle(
        doc_bundle,
        followup_limit=DOC_BUNDLE_MICRO_FOLLOWUP_LIMIT,
        followup_chars=DOC_BUNDLE_MICRO_FOLLOWUP_CHARS,
    )


def _should_compact_initial_free_shell_context(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    shell_hints: dict[str, Any],
    file_inventory: list[dict[str, Any]],
    doc_bundle: list[dict[str, Any]],
) -> bool:
    candidate_text = " ".join(
        [
            str(loaded_skill.skill.name or ""),
            str(loaded_skill.skill.description or ""),
            str(request.goal or ""),
            str(request.user_query or ""),
            " ".join(str(item) for item in request.constraints.values()),
        ]
    )
    is_actionable = bool(ACTIONABLE_SKILL_PATTERN.search(candidate_text)) or bool(
        shell_hints.get("runnable_scripts") or shell_hints.get("documented_scripts")
    )
    if not is_actionable:
        return False
    doc_chars = sum(
        len(str(item.get("content", "")))
        for item in doc_bundle
        if isinstance(item, dict)
    )
    return (
        doc_chars >= FREE_SHELL_LARGE_ACTIONABLE_DOC_CHARS
        or len(file_inventory) >= FREE_SHELL_LARGE_ACTIONABLE_FILE_COUNT
    )


def _summarize_shell_hints_for_free_shell(
    shell_hints: dict[str, Any],
    *,
    runnable_limit: int = 8,
    example_limit: int = 4,
) -> dict[str, Any]:
    runnable_scripts = [
        str(item)
        for item in shell_hints.get("runnable_scripts", [])
        if isinstance(item, str) and str(item).strip()
    ][: max(1, runnable_limit)]
    documented_scripts = [
        str(item)
        for item in shell_hints.get("documented_scripts", [])
        if isinstance(item, str) and str(item).strip()
    ][: max(1, runnable_limit)]
    runnable_script_summaries = [
        dict(item)
        for item in shell_hints.get("runnable_script_summaries", [])
        if isinstance(item, dict)
    ][: max(1, runnable_limit)]
    required_credentials = [
        str(item)
        for item in shell_hints.get("required_credentials", [])
        if isinstance(item, (str, int, float))
    ]
    example_commands = [
        str(item)
        for item in shell_hints.get("example_commands", [])
        if isinstance(item, (str, int, float))
    ][: max(1, example_limit)]
    return {
        "execution_mode": shell_hints.get("execution_mode", "shell"),
        "runnable_scripts": runnable_scripts,
        "documented_scripts": documented_scripts,
        "runnable_script_summaries": runnable_script_summaries,
        "example_commands": example_commands,
        "required_credentials": required_credentials,
        "python_example_count": shell_hints.get("python_example_count", 0),
        "notes": shell_hints.get("notes", ""),
    }


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
        bootstrap_commands=list(plan.bootstrap_commands),
        bootstrap_reason=plan.bootstrap_reason,
        missing_fields=list(plan.missing_fields),
        failure_reason=plan.failure_reason,
        hints=dict(plan.hints),
    )


def _clone_plan_with_hints(
    plan: SkillCommandPlan,
    *,
    hints: dict[str, Any],
) -> SkillCommandPlan:
    return SkillCommandPlan(
        skill_name=plan.skill_name,
        goal=plan.goal,
        user_query=plan.user_query,
        runtime_target=plan.runtime_target,
        constraints=dict(plan.constraints),
        command=plan.command,
        mode=plan.mode,
        shell_mode=plan.shell_mode,
        rationale=plan.rationale,
        entrypoint=plan.entrypoint,
        cli_args=list(plan.cli_args),
        generated_files=list(plan.generated_files),
        bootstrap_commands=list(plan.bootstrap_commands),
        bootstrap_reason=plan.bootstrap_reason,
        missing_fields=list(plan.missing_fields),
        failure_reason=plan.failure_reason,
        hints=hints,
    )


def _is_actionable_skill_request(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
) -> bool:
    constraints = dict(request.constraints or {})
    runtime_requirements = loaded_skill.skill.metadata.get("runtime_requirements")
    has_runtime_requirements = isinstance(runtime_requirements, str) and bool(
        runtime_requirements.strip()
    )
    has_script_inventory = any(item.kind == "script" for item in loaded_skill.file_inventory)
    has_documented_entrypoints = bool(extract_documented_entrypoint_paths(loaded_skill.skill_md))
    if not any([has_runtime_requirements, has_script_inventory, has_documented_entrypoints]):
        return False

    if any(
        key in constraints
        for key in (
            "input_path",
            "input_paths",
            "output_path",
            "output_format",
            "format",
            "topic",
            "title",
            "template",
            "code",
            "codes",
            "ticker",
            "target",
        )
    ):
        return True

    request_text = _join_request_context_text(
        request.goal,
        request.user_query,
        request.constraints,
    )
    if ACTIONABLE_SKILL_PATTERN.search(request_text):
        return True

    skill_text = "\n".join(
        [
            loaded_skill.skill.name,
            loaded_skill.skill.description,
            loaded_skill.skill_md[:6000],
        ]
    )
    return bool(ACTIONABLE_SKILL_PATTERN.search(skill_text))


def _should_attempt_auto_free_shell_plan(
    plan: SkillCommandPlan,
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
) -> bool:
    if not plan.is_manual_required:
        return False
    if not _is_actionable_skill_request(loaded_skill=loaded_skill, request=request):
        return False
    failure_reason = str(plan.failure_reason or "").strip().lower()
    if failure_reason in NON_ESCALATABLE_MANUAL_FAILURE_REASONS:
        return False
    if failure_reason in AUTO_FREE_SHELL_FALLBACK_FAILURE_REASONS:
        return True
    if failure_reason in AUTO_FREE_SHELL_CREATION_ONLY_FAILURE_REASONS:
        return _is_creation_or_generation_request(loaded_skill=loaded_skill, request=request)
    return False


def _build_plan_blockers(plan: SkillCommandPlan) -> list[dict[str, Any]]:
    failure_reason = str(plan.failure_reason or "").strip()
    missing_fields = [str(item) for item in plan.missing_fields if str(item).strip()]
    rationale = str(plan.rationale or "").strip()
    if not any([failure_reason, missing_fields, rationale]):
        return []
    return [
        {
            "failure_reason": failure_reason or None,
            "missing_fields": missing_fields,
            "rationale": rationale or None,
        }
    ]


def _finalize_plan_metadata(
    plan: SkillCommandPlan,
    *,
    requested_shell_mode: SkillShellMode,
    constraint_inference_metadata: dict[str, Any] | None,
    planning_blockers: list[dict[str, Any]] | None = None,
    escalation_reason: str | None = None,
) -> SkillCommandPlan:
    hints = dict(plan.hints)
    inferred_constraints = {}
    if isinstance(constraint_inference_metadata, dict):
        applied = constraint_inference_metadata.get("applied")
        if isinstance(applied, dict):
            inferred_constraints = {
                str(key): value for key, value in applied.items() if str(key).strip()
            }
    blockers = (
        [dict(item) for item in planning_blockers if isinstance(item, dict)]
        if isinstance(planning_blockers, list)
        else _build_plan_blockers(plan)
    )
    effective_shell_mode = normalize_shell_mode(plan.shell_mode)
    requested_normalized = normalize_shell_mode(requested_shell_mode)
    shell_mode_escalated = requested_normalized != effective_shell_mode
    hints["shell_mode_requested"] = requested_normalized
    hints["shell_mode_effective"] = effective_shell_mode
    hints["shell_mode_escalated"] = shell_mode_escalated
    if escalation_reason:
        hints["shell_mode_escalation_reason"] = escalation_reason
    elif shell_mode_escalated and "shell_mode_escalation_reason" not in hints:
        hints["shell_mode_escalation_reason"] = str(plan.failure_reason or "").strip() or None
    if inferred_constraints:
        hints["auto_inferred_constraints"] = inferred_constraints
    if blockers:
        hints["planning_blockers"] = blockers
    if plan.is_manual_required:
        hints["manual_required_kind"] = hints.get("manual_required_kind") or classify_manual_required_kind(
            plan.failure_reason
        )
        hints["planner_error_summary"] = hints.get("planner_error_summary") or summarize_planner_error(
            str(plan.failure_reason or "").strip(),
            str(plan.rationale or "").strip(),
            *(str(item) for item in hints.get("warnings", []) if isinstance(item, str)),
        )
    return _clone_plan_with_hints(plan, hints=hints)


def _is_creation_or_generation_request(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
) -> bool:
    constraints = dict(request.constraints or {})
    if extract_input_paths(constraints):
        return False
    if any(
        key in constraints
        for key in (
            "topic",
            "title",
            "output_format",
            "slide_count",
            "style",
            "theme",
            "language",
            "audience",
            "template",
        )
    ):
        return True
    query_text = "\n".join(
        [
            str(request.goal or ""),
            str(request.user_query or ""),
            loaded_skill.skill.name,
        ]
    )
    return bool(CREATION_REQUEST_PATTERN.search(query_text))


def _build_preferred_shipped_script_plan(
    *,
    loaded_skill: LoadedSkill,
    request: SkillExecutionRequest,
    shell_hints: dict[str, Any],
    shell_mode: SkillShellMode,
) -> SkillCommandPlan | None:
    query_text = _join_request_context_text(
        request.goal,
        request.user_query,
        request.constraints,
    )
    documented_scripts = {
        item.replace("\\", "/").strip()
        for item in shell_hints.get("documented_scripts", [])
        if isinstance(item, str) and item.strip()
    }
    candidate_runnable_scripts = [
        item.replace("\\", "/").strip()
        for item in shell_hints.get("runnable_scripts", [])
        if isinstance(item, str) and item.strip()
    ]
    previews_by_path = {
        str(item.get("path")): int(item.get("score", 0))
        for item in load_script_previews(
            loaded_skill,
            query_text=query_text,
            limit=8,
            candidate_paths=candidate_runnable_scripts,
        )
        if isinstance(item, dict) and item.get("path")
    }
    financial_request_kind = (
        _infer_financial_research_request_kind(
            goal=request.goal,
            user_query=request.user_query,
            constraints=request.constraints,
        )
        if loaded_skill.skill.name.strip().lower() == "financial-researching"
        else None
    )
    candidates: list[dict[str, Any]] = []
    for index, relative_path in enumerate(candidate_runnable_scripts):
        if documented_scripts and relative_path not in documented_scripts:
            continue
        skip_candidate = False
        intent_bonus = 0
        if loaded_skill.skill.name.strip().lower() == "financial-researching":
            skip_candidate, intent_bonus = _score_financial_research_candidate(
                relative_path=relative_path,
                request_kind=financial_request_kind,
            )
        if skip_candidate:
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
        relevance_score = _score_relevance(relative_path, query_text) + preview_score + intent_bonus
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

def _build_generational_ai_outline(
    *,
    title: str,
    topic: str,
    language: str,
) -> list[dict[str, Any]]:
    if language == "zh":
        return [
            {
                "kind": "title",
                "title": title,
                "subtitle": "从概率生成模型到多模态基础模型的演进脉络",
                "kicker": "AI / HISTORY / EVOLUTION",
            },
            {
                "kind": "content",
                "title": "为什么生成式人工智能成为新一轮技术平台",
                "bullets": [
                    "生成式人工智能的核心不只是“会生成内容”，而是把知识压缩、推理和交互整合到统一模型中。",
                    "算力、数据和 Transformer 架构的叠加，显著提升了模型的通用性、质量和可部署性。",
                    "产品形态正在从“单点生成”走向“工具调用 + Agent 协作 + 多模态工作流”。",
                ],
            },
            {
                "kind": "content",
                "title": "理论起点：从统计生成到早期神经网络",
                "bullets": [
                    "20 世纪中后期，统计语言模型、概率图模型和隐变量方法奠定了“生成”问题的数学基础。",
                    "玻尔兹曼机、自编码思想和早期连接主义尝试，为后续深度生成模型提供了结构启发。",
                    "当时的主要瓶颈在于数据规模、算力供给和训练稳定性，导致生成质量长期受限。",
                ],
            },
            {
                "kind": "content",
                "title": "深度学习复兴：表示学习重新激活生成能力",
                "bullets": [
                    "2006 年后，深层网络训练技巧改进，让表征学习从手工特征转向端到端学习。",
                    "大规模数据集与 GPU 训练基础设施逐步成熟，模型开始具备跨任务迁移能力。",
                    "生成模型由“研究样例”走向“可复用组件”，为之后的突破积累条件。",
                ],
            },
            {
                "kind": "content",
                "title": "关键跃迁：VAE、GAN、Seq2Seq 与 Attention",
                "bullets": [
                    "VAE 把生成过程与潜变量建模结合，提升了连续潜空间表达能力。",
                    "GAN 显著提高了图像生成的逼真度，并推动了生成质量评价标准的变化。",
                    "Seq2Seq 与 Attention 让文本生成从短句拓展到翻译、摘要和对话等复杂任务。",
                ],
            },
            {
                "kind": "content",
                "title": "基础模型时代：Transformer 与 GPT 系列",
                "bullets": [
                    "Transformer 把长距离依赖建模效率提升到可大规模预训练的水平。",
                    "GPT、BERT 等预训练范式证明：统一大模型可以覆盖理解、生成与推理任务。",
                    "从参数规模到指令对齐，再到工具使用，模型能力开始呈现平台化增长。",
                ],
            },
            {
                "kind": "content",
                "title": "多模态扩展：从文本走向图像、音频与视频",
                "bullets": [
                    "扩散模型推动图像生成质量和可控性大幅提升，重塑创意生产流程。",
                    "语音合成、音乐生成和视频生成把生成式 AI 从文本助手扩展为内容引擎。",
                    "多模态统一模型正成为下一阶段竞争焦点，目标是共享表示与跨模态协同。",
                ],
            },
            {
                "kind": "content",
                "title": "产业落地：办公、研发、营销与行业场景",
                "bullets": [
                    "在办公场景中，生成式 AI 提升检索、写作、汇报与知识复用效率。",
                    "在软件研发中，代码补全、测试生成和文档维护正形成新的开发范式。",
                    "金融、教育、医疗和制造等行业则更关注可解释性、合规性与流程嵌入。",
                ],
            },
            {
                "kind": "content",
                "title": "风险与治理：幻觉、版权、安全与成本",
                "bullets": [
                    "事实幻觉与推理不稳定仍然是高价值业务流程落地的核心风险。",
                    "训练数据版权、隐私边界和模型输出责任，要求企业建立明确的治理机制。",
                    "部署成本、推理延迟和评测体系，决定了大模型从试点到规模化的速度。",
                ],
            },
            {
                "kind": "content",
                "title": "下一阶段：Agent 化、工具调用与行业专用模型",
                "bullets": [
                    "未来竞争重点会从“单模型能力”转向“模型 + 工具 + 工作流”的系统能力。",
                    "Agent 将承担规划、检索、执行和反馈闭环，使模型更接近可交付的生产力单元。",
                    f"{topic} 的叙事主线，可以概括为：理论突破、工程放大、产品化扩散与治理同步演进。",
                ],
            },
        ]
    return [
        {
            "kind": "title",
            "title": title,
            "subtitle": "From probabilistic modeling to multimodal foundation models",
            "kicker": "AI / HISTORY / EVOLUTION",
        },
        {
            "kind": "content",
            "title": "Why generative AI became a platform shift",
            "bullets": [
                "Generative AI combines content generation, reasoning, retrieval, and interaction inside unified models.",
                "The convergence of data scale, GPU infrastructure, and Transformer architectures unlocked broad generality.",
                "The market is moving from isolated generation tools to agentic, multimodal production workflows.",
            ],
        },
        {
            "kind": "content",
            "title": "Origins: statistical generation and early neural ideas",
            "bullets": [
                "Statistical language models, probabilistic graphs, and latent-variable methods framed generation mathematically.",
                "Boltzmann machines and early autoencoding ideas seeded later deep generative architectures.",
                "Limited compute and data kept early systems narrow, unstable, and difficult to scale.",
            ],
        },
        {
            "kind": "content",
            "title": "Deep learning revival reactivated generation",
            "bullets": [
                "Training improvements after 2006 made deeper representation learning practical again.",
                "Large datasets and GPU stacks turned generative methods into reusable building blocks.",
                "End-to-end learning reduced handcrafted features and improved transfer across tasks.",
            ],
        },
        {
            "kind": "content",
            "title": "Milestones: VAE, GAN, Seq2Seq, and Attention",
            "bullets": [
                "VAEs linked generation to structured latent spaces and stable optimization.",
                "GANs sharply improved perceptual realism, especially for image synthesis.",
                "Seq2Seq and attention expanded text generation into translation, summarization, and dialogue.",
            ],
        },
        {
            "kind": "content",
            "title": "Foundation model era: Transformer and GPT",
            "bullets": [
                "Transformers made large-scale pretraining and long-range dependency modeling efficient.",
                "Pretraining showed that one large model family could support understanding and generation together.",
                "Instruction tuning and tool use turned model scale into a product platform.",
            ],
        },
        {
            "kind": "content",
            "title": "Multimodal expansion beyond text",
            "bullets": [
                "Diffusion models pushed image quality and controllability to production-ready levels.",
                "Speech, music, and video generation extended the category from assistant to content engine.",
                "Shared multimodal representations now define the next frontier of model competition.",
            ],
        },
        {
            "kind": "content",
            "title": "Where value is created today",
            "bullets": [
                "Knowledge work, software delivery, and marketing workflows are the fastest-moving adoption zones.",
                "Industry deployments focus on reliability, compliance, and deep workflow integration.",
                "The strongest value comes from combining models with enterprise context and execution tools.",
            ],
        },
        {
            "kind": "content",
            "title": "Risk and governance remain decisive",
            "bullets": [
                "Hallucinations, unstable reasoning, and weak evaluation can block high-stakes deployment.",
                "Copyright, privacy, and accountability rules shape how models are trained and used.",
                "Inference cost, latency, and observability determine whether pilots become operating systems.",
            ],
        },
        {
            "kind": "content",
            "title": "What comes next",
            "bullets": [
                "Competition is shifting from single-model capability to system capability.",
                "Agentic orchestration and tool use will define the next wave of enterprise leverage.",
                f"The arc of {topic} is best read as theory, scale, productization, and governance evolving together.",
            ],
        },
    ]


def _build_generic_pptx_outline(
    *,
    title: str,
    topic: str,
    language: str,
) -> list[dict[str, Any]]:
    if language == "zh":
        return [
            {
                "kind": "title",
                "title": title,
                "subtitle": "结构化概览、关键判断与行动建议",
                "kicker": "AUTO-GENERATED DECK",
            },
            {
                "kind": "content",
                "title": "主题概览",
                "bullets": [
                    f"{topic} 的分析应先统一定义边界、对象和业务目标。",
                    "高质量演示文稿需要把背景、现状、核心问题和行动建议拆分为独立页面。",
                    "本 deck 采用“背景 - 分析 - 价值 - 风险 - 展望”的通用结构。",
                ],
            },
            {
                "kind": "content",
                "title": "背景与驱动因素",
                "bullets": [
                    "先明确宏观环境、技术条件或市场变化，再解释主题为何值得关注。",
                    "驱动因素通常来自需求变化、成本结构变化和能力边界变化。",
                    "这一页的重点是建立问题的重要性，而不是提前给出答案。",
                ],
            },
            {
                "kind": "content",
                "title": "当前格局",
                "bullets": [
                    "用 3 到 4 个维度描述现状，例如参与者、流程、工具、竞争结构或用户行为。",
                    "避免无结构堆信息，优先呈现可比较、可归类、可复述的框架。",
                    "这一页承担“把复杂现状变成可管理结构”的作用。",
                ],
            },
            {
                "kind": "content",
                "title": "关键方法与构成模块",
                "bullets": [
                    "拆出该主题的核心方法、流程节点或能力模块，让听众知道系统如何运转。",
                    "如果存在输入、处理、输出链路，建议按链路顺序组织信息。",
                    "把抽象概念翻译成流程和模块，能显著提升可理解性。",
                ],
            },
            {
                "kind": "content",
                "title": "价值创造点",
                "bullets": [
                    "从效率、质量、收入、风险控制或体验提升等维度说明实际收益。",
                    "尽量避免空泛表述，用“对谁、改善什么、为什么成立”来组织要点。",
                    "这一页的目标是让听众形成业务上的“值得做”判断。",
                ],
            },
            {
                "kind": "content",
                "title": "主要挑战与约束",
                "bullets": [
                    "同时呈现机会和限制，能提升 deck 的可信度和决策价值。",
                    "约束可能来自数据、流程、组织协同、法规、成本或技术成熟度。",
                    "建议把挑战写成“风险点 + 影响 + 应对方向”的形式。",
                ],
            },
            {
                "kind": "content",
                "title": "实施路径",
                "bullets": [
                    "把落地方案拆成阶段性动作，例如试点、验证、扩展和运营优化。",
                    "为每个阶段定义目标、关键资源和验收标准，降低执行模糊度。",
                    "这样能把抽象主题转成可推进的行动方案。",
                ],
            },
            {
                "kind": "content",
                "title": "未来趋势",
                "bullets": [
                    "从技术、竞争、用户预期和政策环境四个维度看下一阶段变化。",
                    "趋势页不是预测细节，而是帮助听众形成方向性判断。",
                    "建议把趋势与当前能力建设的优先级联系起来。",
                ],
            },
            {
                "kind": "content",
                "title": "总结",
                "bullets": [
                    f"{topic} 的核心价值，在于把复杂问题压缩成统一框架和可执行路径。",
                    "一份好的总结页应同时回答：现在发生了什么、为什么重要、接下来做什么。",
                    "如果需要继续深化，可在此基础上扩展案例、数据或实施细节。",
                ],
            },
        ]
    return [
        {
            "kind": "title",
            "title": title,
            "subtitle": "A structured overview with key judgments and action points",
            "kicker": "AUTO-GENERATED DECK",
        },
        {
            "kind": "content",
            "title": "Topic overview",
            "bullets": [
                f"{topic} is easiest to explain when the scope, audience, and decision goal are made explicit first.",
                "A strong presentation separates context, current state, core mechanisms, and next actions.",
                "This deck follows a background, analysis, value, risk, and outlook structure.",
            ],
        },
        {
            "kind": "content",
            "title": "Context and drivers",
            "bullets": [
                "Start with the forces that make the topic important right now.",
                "Typical drivers include user demand, cost structure shifts, regulation, and technical capability changes.",
                "The job of this slide is to establish urgency before giving recommendations.",
            ],
        },
        {
            "kind": "content",
            "title": "Current landscape",
            "bullets": [
                "Describe the current state through a small set of stable dimensions.",
                "Turn a messy situation into a frame that can be compared, repeated, and discussed.",
                "This creates a shared map for the rest of the deck.",
            ],
        },
        {
            "kind": "content",
            "title": "Core methods and building blocks",
            "bullets": [
                "Break the topic into the major processes, capabilities, or system components.",
                "If there is an input-process-output chain, follow that order.",
                "Concrete modules are easier to discuss than abstract claims.",
            ],
        },
        {
            "kind": "content",
            "title": "Where value is created",
            "bullets": [
                "Explain the value in terms of efficiency, quality, growth, control, or user experience.",
                "Each point should answer who benefits, what changes, and why it matters.",
                "This is the slide that converts analysis into business relevance.",
            ],
        },
        {
            "kind": "content",
            "title": "Constraints and risks",
            "bullets": [
                "A credible deck shows both upside and friction.",
                "Constraints may come from data, workflow design, cost, policy, or organizational readiness.",
                "Frame each issue as a risk, its effect, and a response direction.",
            ],
        },
        {
            "kind": "content",
            "title": "Execution path",
            "bullets": [
                "Turn the topic into staged actions such as pilot, validation, rollout, and optimization.",
                "Each stage should have a goal, key inputs, and a decision checkpoint.",
                "This reduces ambiguity and helps the audience move from interest to action.",
            ],
        },
        {
            "kind": "content",
            "title": "Future outlook",
            "bullets": [
                "Look ahead through technology, competition, regulation, and user behavior.",
                "The point is not precision but directional clarity.",
                "Link the outlook to what should be built or prioritized now.",
            ],
        },
        {
            "kind": "content",
            "title": "Summary",
            "bullets": [
                f"{topic} becomes more actionable when it is reduced to a clear frame and operating plan.",
                "A good close answers what changed, why it matters, and what should happen next.",
                "From here, the deck can be extended with data, cases, or implementation detail.",
            ],
        },
    ]


def _fit_outline_to_slide_count(
    slides: list[dict[str, Any]],
    *,
    slide_count: int,
) -> list[dict[str, Any]]:
    if slide_count >= len(slides):
        extras = [
            {
                "kind": "content",
                "title": f"附录 {index}" if _looks_like_chinese(slides[0].get("title", "")) else f"Appendix {index}",
                "bullets": [
                    "补充延展案例，用于支持主线判断。" if _looks_like_chinese(slides[0].get("title", "")) else "Extended supporting example for the main narrative.",
                    "可进一步加入数据、图表或代表性案例。" if _looks_like_chinese(slides[0].get("title", "")) else "Add data, charts, or representative cases here.",
                    "该页作为强默认生成的占位扩展页面。" if _looks_like_chinese(slides[0].get("title", "")) else "This page acts as a default expansion slot.",
                ],
            }
            for index in range(1, slide_count - len(slides) + 1)
        ]
        return [*slides, *extras]
    if slide_count <= 1:
        return slides[:1]
    if slide_count == 2:
        return [slides[0], slides[-1]]
    middle = slides[1:-1]
    keep_middle = middle[: max(0, slide_count - 2)]
    return [slides[0], *keep_middle, slides[-1]]


def _build_pptx_outline(
    *,
    title: str,
    topic: str,
    language: str,
    slide_count: int,
    request_text: str,
) -> list[dict[str, Any]]:
    if PPTX_GENERATIVE_AI_PATTERN.search(request_text) or PPTX_HISTORY_REQUEST_PATTERN.search(
        request_text
    ):
        base = _build_generational_ai_outline(
            title=title,
            topic=topic,
            language=language,
        )
    else:
        base = _build_generic_pptx_outline(
            title=title,
            topic=topic,
            language=language,
        )
    return _fit_outline_to_slide_count(base, slide_count=slide_count)


def _build_pptx_fallback_script(
    *,
    config: dict[str, Any],
    slides: list[dict[str, Any]],
) -> str:
    script_template = textwrap.dedent(
        r"""
        from __future__ import annotations

        import base64
        import io
        import json
        import re
        import zipfile
        from datetime import datetime, timezone
        from pathlib import Path
        from xml.sax.saxutils import escape

        CONFIG = json.loads(r'''__CONFIG_JSON__''')
        SLIDES = json.loads(r'''__SLIDES_JSON__''')
        SLIDE_WIDTH = 9144000
        SLIDE_HEIGHT = 5143500
        OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
        SLIDE_LAYOUT_REL = f"{OFFICE_REL_NS}/slideLayout"
        SLIDE_REL = f"{OFFICE_REL_NS}/slide"
        LANG_TAG = "zh-CN" if str(CONFIG.get("language", "en")).lower().startswith("zh") else "en-US"
        LATIN_FONT = "Aptos"
        EAST_ASIA_FONT = "Microsoft YaHei" if LANG_TAG.startswith("zh") else "Aptos"

        def _emu(inches: float) -> int:
            return int(round(inches * 914400))

        def _run_props(size: int, color: str, *, bold: bool = False) -> str:
            bold_attr = ' b="1"' if bold else ""
            return (
                f'<a:rPr lang="{LANG_TAG}" sz="{size}"{bold_attr}>'
                f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
                f'<a:latin typeface="{LATIN_FONT}"/>'
                f'<a:ea typeface="{EAST_ASIA_FONT}"/>'
                f'<a:cs typeface="{LATIN_FONT}"/>'
                f'</a:rPr>'
            )

        def _paragraph(text: str, *, size: int, color: str, bold: bool = False, bullet: bool = False, align: str = "l") -> str:
            escaped = escape(str(text or ""))
            if bullet:
                ppr = f'<a:pPr marL="{_emu(0.42)}" indent="-{_emu(0.18)}" algn="{align}"><a:buChar char="•"/></a:pPr>'
            else:
                ppr = f'<a:pPr algn="{align}"><a:buNone/></a:pPr>'
            rpr = _run_props(size=size, color=color, bold=bold)
            return f'<a:p>{ppr}<a:r>{rpr}<a:t>{escaped}</a:t></a:r><a:endParaRPr lang="{LANG_TAG}" sz="{size}"/></a:p>'

        def _textbox(shape_id: int, name: str, *, x: int, y: int, cx: int, cy: int, paragraphs: list[str], fill: str | None = None, line: str | None = None) -> str:
            fill_xml = (
                f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
                if fill
                else '<a:noFill/>'
            )
            line_xml = (
                f'<a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>'
                if line
                else '<a:ln><a:noFill/></a:ln>'
            )
            body = ''.join(paragraphs) or f'<a:p><a:endParaRPr lang="{LANG_TAG}"/></a:p>'
            return (
                '<p:sp>'
                '<p:nvSpPr>'
                f'<p:cNvPr id="{shape_id}" name="{escape(name)}"/>'
                '<p:cNvSpPr/>'
                '<p:nvPr/>'
                '</p:nvSpPr>'
                '<p:spPr>'
                f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                f'{fill_xml}{line_xml}'
                '</p:spPr>'
                '<p:txBody>'
                '<a:bodyPr wrap="square" rtlCol="0"><a:normAutofit/></a:bodyPr>'
                '<a:lstStyle/>'
                f'{body}'
                '</p:txBody>'
                '</p:sp>'
            )

        def _shape(shape_id: int, name: str, *, x: int, y: int, cx: int, cy: int, fill: str, line: str | None = None) -> str:
            return (
                '<p:sp>'
                '<p:nvSpPr>'
                f'<p:cNvPr id="{shape_id}" name="{escape(name)}"/>'
                '<p:cNvSpPr/>'
                '<p:nvPr/>'
                '</p:nvSpPr>'
                '<p:spPr>'
                f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
                + (f'<a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>' if line else '<a:ln><a:noFill/></a:ln>')
                + '</p:spPr>'
                '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody>'
                '</p:sp>'
            )

        def _title_slide_xml(slide: dict[str, object], index: int) -> str:
            palette = dict(CONFIG["palette"])
            shapes = [
                _shape(2, f"Background {index}", x=0, y=0, cx=SLIDE_WIDTH, cy=SLIDE_HEIGHT, fill=palette["dark"]),
                _shape(3, f"Accent {index}", x=_emu(0.65), y=_emu(0.85), cx=_emu(1.4), cy=_emu(0.12), fill=palette["accent"]),
                _textbox(
                    4,
                    f"Title {index}",
                    x=_emu(0.8),
                    y=_emu(1.2),
                    cx=_emu(8.4),
                    cy=_emu(1.2),
                    paragraphs=[_paragraph(str(slide.get("title", "")), size=3000, color=palette["text_light"], bold=True)],
                ),
                _textbox(
                    5,
                    f"Subtitle {index}",
                    x=_emu(0.82),
                    y=_emu(2.55),
                    cx=_emu(7.7),
                    cy=_emu(1.4),
                    paragraphs=[_paragraph(str(slide.get("subtitle", "")), size=1800, color="E6E8EB")],
                ),
                _textbox(
                    6,
                    f"Kicker {index}",
                    x=_emu(0.82),
                    y=_emu(4.65),
                    cx=_emu(2.8),
                    cy=_emu(0.42),
                    paragraphs=[_paragraph(str(slide.get("kicker", "AUTO-GENERATED DECK")), size=1100, color=palette["accent_soft"], bold=True)],
                ),
            ]
            return _slide_document(shapes)

        def _content_slide_xml(slide: dict[str, object], index: int) -> str:
            palette = dict(CONFIG["palette"])
            bullet_paragraphs = []
            for bullet in list(slide.get("bullets", []) or [])[:4]:
                bullet_paragraphs.append(_paragraph(str(bullet), size=1700, color=palette["text_dark"], bullet=True))
            if not bullet_paragraphs:
                bullet_paragraphs.append(_paragraph("Content pending.", size=1700, color=palette["text_dark"]))
            shapes = [
                _shape(2, f"Background {index}", x=0, y=0, cx=SLIDE_WIDTH, cy=SLIDE_HEIGHT, fill=palette["light"]),
                _shape(3, f"Header {index}", x=0, y=0, cx=SLIDE_WIDTH, cy=_emu(0.7), fill=palette["dark"]),
                _shape(4, f"Accent Block {index}", x=_emu(0.82), y=_emu(1.1), cx=_emu(0.18), cy=_emu(2.9), fill=palette["accent"]),
                _textbox(
                    5,
                    f"Title {index}",
                    x=_emu(0.82),
                    y=_emu(0.14),
                    cx=_emu(7.0),
                    cy=_emu(0.36),
                    paragraphs=[_paragraph(str(slide.get("title", "")), size=2200, color=palette["text_light"], bold=True)],
                ),
                _textbox(
                    6,
                    f"Slide Number {index}",
                    x=_emu(8.65),
                    y=_emu(0.16),
                    cx=_emu(0.42),
                    cy=_emu(0.32),
                    paragraphs=[_paragraph(f"{index}", size=1200, color=palette["accent_soft"], bold=True, align="ctr")],
                ),
                _textbox(
                    7,
                    f"Bullets {index}",
                    x=_emu(1.15),
                    y=_emu(1.05),
                    cx=_emu(7.4),
                    cy=_emu(3.35),
                    paragraphs=bullet_paragraphs,
                ),
                _textbox(
                    8,
                    f"Footer {index}",
                    x=_emu(1.15),
                    y=_emu(4.55),
                    cx=_emu(7.0),
                    cy=_emu(0.38),
                    paragraphs=[_paragraph(str(CONFIG.get("topic", "")), size=1000, color=palette["muted"])],
                ),
            ]
            return _slide_document(shapes)

        def _slide_document(shapes: list[str]) -> str:
            joined = ''.join(shapes)
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                '<p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
                f'{joined}'
                '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
            )

        def _slide_relationship_xml() -> str:
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Relationships xmlns="{PACKAGE_REL_NS}">'
                f'<Relationship Id="rId1" Type="{SLIDE_LAYOUT_REL}" Target="../slideLayouts/slideLayout7.xml"/>'
                '</Relationships>'
            )

        def _build_content_types_xml(slide_count: int) -> str:
            overrides = ''.join(
                f'<Override PartName="/ppt/slides/slide{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
                for index in range(1, slide_count + 1)
            )
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Default Extension="jpeg" ContentType="image/jpeg"/>'
                '<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.presentationml.printerSettings"/>'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
                '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
                '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
                '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
                '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout2.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout3.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout4.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout5.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout6.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout7.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout8.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout9.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout10.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/ppt/slideLayouts/slideLayout11.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                f'{overrides}'
                '</Types>'
            )

        def _build_app_xml(slides: list[dict[str, object]]) -> str:
            titles = ''.join(f'<vt:lpstr>{escape(str(item.get("title", "")))}</vt:lpstr>' for item in slides)
            titles_size = len(slides) + 1
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                '<TotalTime>1</TotalTime><Words>0</Words><Application>OpenAI Codex PPTX Fallback</Application>'
                '<PresentationFormat>On-screen Show (16:9)</PresentationFormat>'
                '<Paragraphs>0</Paragraphs>'
                f'<Slides>{len(slides)}</Slides>'
                '<Notes>0</Notes><HiddenSlides>0</HiddenSlides><MMClips>0</MMClips><ScaleCrop>false</ScaleCrop>'
                '<HeadingPairs><vt:vector size="4" baseType="variant">'
                '<vt:variant><vt:lpstr>Theme</vt:lpstr></vt:variant>'
                '<vt:variant><vt:i4>1</vt:i4></vt:variant>'
                '<vt:variant><vt:lpstr>Slide Titles</vt:lpstr></vt:variant>'
                f'<vt:variant><vt:i4>{len(slides)}</vt:i4></vt:variant>'
                '</vt:vector></HeadingPairs>'
                f'<TitlesOfParts><vt:vector size="{titles_size}" baseType="lpstr"><vt:lpstr>Office Theme</vt:lpstr>{titles}</vt:vector></TitlesOfParts>'
                '<Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>'
                '<HyperlinkBase></HyperlinkBase><HyperlinksChanged>false</HyperlinksChanged><AppVersion>16.0000</AppVersion>'
                '</Properties>'
            )

        def _build_core_xml() -> str:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            title = escape(str(CONFIG.get("title", "")))
            subject = escape(str(CONFIG.get("topic", "")))
            description = escape(str(CONFIG.get("description", "")))
            return (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f'<dc:title>{title}</dc:title><dc:subject>{subject}</dc:subject>'
                '<dc:creator>OpenAI Codex</dc:creator><cp:keywords></cp:keywords>'
                f'<dc:description>{description}</dc:description><cp:lastModifiedBy>OpenAI Codex</cp:lastModifiedBy>'
                '<cp:revision>1</cp:revision>'
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
                '<cp:category></cp:category></cp:coreProperties>'
            )

        def _update_presentation_xml(presentation_xml: str, slide_count: int) -> str:
            slide_id_list = '<p:sldIdLst>' + ''.join(
                f'<p:sldId id="{255 + index}" r:id="rId{99 + index}"/>'
                for index in range(1, slide_count + 1)
            ) + '</p:sldIdLst>'
            if '<p:sldIdLst>' in presentation_xml:
                presentation_xml = re.sub(r'<p:sldIdLst>.*?</p:sldIdLst>', slide_id_list, presentation_xml, count=1)
            else:
                presentation_xml = presentation_xml.replace('</p:sldMasterIdLst>', f'</p:sldMasterIdLst>{slide_id_list}', 1)
            return re.sub(
                r'<p:sldSz cx="\d+" cy="\d+" type="[^"]*"/>',
                '<p:sldSz cx="9144000" cy="5143500" type="screen16x9"/>',
                presentation_xml,
                count=1,
            )

        def _update_presentation_rels_xml(rels_xml: str, slide_count: int) -> str:
            slide_rels = ''.join(
                f'<Relationship Id="rId{99 + index}" Type="{SLIDE_REL}" Target="slides/slide{index}.xml"/>'
                for index in range(1, slide_count + 1)
            )
            return rels_xml.replace('</Relationships>', f'{slide_rels}</Relationships>', 1)

        def _build_outline_markdown(slides: list[dict[str, object]]) -> str:
            lines = [f"# {CONFIG['title']}", ""]
            for idx, slide in enumerate(slides, start=1):
                lines.append(f"## {idx}. {slide.get('title', '')}")
                subtitle = str(slide.get("subtitle", "") or "").strip()
                if subtitle:
                    lines.append(subtitle)
                for bullet in list(slide.get("bullets", []) or []):
                    lines.append(f"- {bullet}")
                lines.append("")
            return "\n".join(lines).strip() + "\n"

        def main() -> None:
            output_path = Path(str(CONFIG["output_path"]))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            outline_path = Path(str(CONFIG["outline_path"]))
            outline_path.parent.mkdir(parents=True, exist_ok=True)
            template_b64 = Path(__file__).with_name("template.b64").read_text(encoding="ascii")

            template_entries: dict[str, bytes] = {}
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(template_b64))) as template_zip:
                for info in template_zip.infolist():
                    template_entries[info.filename] = template_zip.read(info.filename)

            template_entries["[Content_Types].xml"] = _build_content_types_xml(len(SLIDES)).encode("utf-8")
            template_entries["ppt/presentation.xml"] = _update_presentation_xml(
                template_entries["ppt/presentation.xml"].decode("utf-8"),
                len(SLIDES),
            ).encode("utf-8")
            template_entries["ppt/_rels/presentation.xml.rels"] = _update_presentation_rels_xml(
                template_entries["ppt/_rels/presentation.xml.rels"].decode("utf-8"),
                len(SLIDES),
            ).encode("utf-8")
            template_entries["docProps/app.xml"] = _build_app_xml(SLIDES).encode("utf-8")
            template_entries["docProps/core.xml"] = _build_core_xml().encode("utf-8")

            slide_rels_xml = _slide_relationship_xml().encode("utf-8")
            for index, slide in enumerate(SLIDES, start=1):
                if str(slide.get("kind", "")).strip().lower() == "title":
                    slide_xml = _title_slide_xml(slide, index)
                else:
                    slide_xml = _content_slide_xml(slide, index)
                template_entries[f"ppt/slides/slide{index}.xml"] = slide_xml.encode("utf-8")
                template_entries[f"ppt/slides/_rels/slide{index}.xml.rels"] = slide_rels_xml

            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as out_zip:
                for name in sorted(template_entries):
                    out_zip.writestr(name, template_entries[name])

            outline_path.write_text(_build_outline_markdown(SLIDES), encoding="utf-8")
            print(f"Created {output_path.as_posix()}")
            print(f"Outline {outline_path.as_posix()}")

        if __name__ == "__main__":
            main()
        """
    ).strip()
    script = script_template.replace(
        "__CONFIG_JSON__",
        json.dumps(config, ensure_ascii=False, indent=2),
    )
    script = script.replace(
        "__SLIDES_JSON__",
        json.dumps(slides, ensure_ascii=False, indent=2),
    )
    return script + "\n"


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

        skill_md_excerpt = loaded_skill.skill_md
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
        requested_shell_mode = resolve_shell_mode(
            dict(request.constraints or {}),
            default=self.default_shell_mode,
        )
        shell_mode = requested_shell_mode
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
            if not _should_attempt_auto_free_shell_plan(
                conservative_plan,
                loaded_skill=loaded_skill,
                request=normalized_request,
            ):
                return _finalize_plan_metadata(
                    conservative_plan,
                    requested_shell_mode=requested_shell_mode,
                    constraint_inference_metadata=constraint_inference_metadata,
                )
            planning_blockers = _build_plan_blockers(conservative_plan)
            free_shell_request = SkillExecutionRequest(
                skill_name=normalized_request.skill_name,
                goal=normalized_request.goal,
                user_query=normalized_request.user_query,
                workspace=normalized_request.workspace,
                shell_mode="free_shell",
                runtime_target=normalized_request.runtime_target,
                constraints=dict(normalized_request.constraints),
            )
            free_shell_plan = await self._plan_free_shell(
                loaded_skill=loaded_skill,
                request=free_shell_request,
                shell_hints=shell_hints,
                fallback_plan=conservative_plan,
            )
            if not free_shell_plan.is_manual_required:
                return _finalize_plan_metadata(
                    free_shell_plan,
                    requested_shell_mode=requested_shell_mode,
                    constraint_inference_metadata=constraint_inference_metadata,
                    planning_blockers=planning_blockers,
                    escalation_reason=str(conservative_plan.failure_reason or "").strip()
                    or "auto_free_shell_fallback",
                )
            return _finalize_plan_metadata(
                free_shell_plan,
                requested_shell_mode=requested_shell_mode,
                constraint_inference_metadata=constraint_inference_metadata,
                planning_blockers=planning_blockers,
                escalation_reason=str(conservative_plan.failure_reason or "").strip()
                or "auto_free_shell_fallback",
            )
        preferred_shipped_plan = _build_preferred_shipped_script_plan(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
            shell_mode=shell_mode,
        )
        if preferred_shipped_plan is not None:
            return _finalize_plan_metadata(
                preferred_shipped_plan,
                requested_shell_mode=requested_shell_mode,
                constraint_inference_metadata=constraint_inference_metadata,
            )
        free_shell_plan = await self._plan_free_shell(
            loaded_skill=loaded_skill,
            request=normalized_request,
            shell_hints=shell_hints,
            fallback_plan=conservative_plan,
        )
        if not free_shell_plan.is_manual_required:
            return _finalize_plan_metadata(
                free_shell_plan,
                requested_shell_mode=requested_shell_mode,
                constraint_inference_metadata=constraint_inference_metadata,
            )
        if (
            not conservative_plan.is_manual_required
            and free_shell_plan.failure_reason
            in {"llm_not_available_for_free_shell", "llm_planning_failed"}
        ):
            free_shell_fallback_hints = dict(free_shell_plan.hints)
            free_shell_fallback_hints.setdefault("planner", "free_shell")
            return _finalize_plan_metadata(
                _clone_plan_with_hints(
                    _clone_plan_with_shell_mode(
                        conservative_plan,
                        shell_mode="free_shell",
                    ),
                    hints={
                        **free_shell_fallback_hints,
                        **dict(conservative_plan.hints),
                        "planner": free_shell_fallback_hints.get("planner", "free_shell"),
                    },
                ),
                requested_shell_mode=requested_shell_mode,
                constraint_inference_metadata=constraint_inference_metadata,
            )
        return _finalize_plan_metadata(
            free_shell_plan,
            requested_shell_mode=requested_shell_mode,
            constraint_inference_metadata=constraint_inference_metadata,
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
                failure_reason="llm_not_available_for_free_shell",
                missing_fields=[],
                rationale=(
                    "Free-shell planning requires an available utility LLM because the skill "
                    "cannot be reduced to the conservative single-entrypoint planner."
                ),
                planner_error_summary="No utility LLM is available for free-shell planning.",
                manual_required_kind="technical_blocked",
            )

        query_text = "\n".join(
            [
                request.goal,
                request.user_query,
                " ".join(str(item) for item in request.constraints.values()),
            ]
        )
        effective_constraints = dict(request.constraints)
        python_examples = select_relevant_examples(
            extract_python_examples(loaded_skill.skill_md),
            query_text=query_text,
            limit=MAX_FREE_SHELL_EXAMPLE_COUNT,
            max_chars=MAX_FREE_SHELL_EXAMPLE_CHARS,
            max_total_chars=MAX_FREE_SHELL_TOTAL_EXAMPLE_CHARS,
        )
        candidate_runnable_scripts = [
            item.replace("\\", "/").strip()
            for item in shell_hints.get("runnable_scripts", [])
            if isinstance(item, str) and item.strip()
        ]
        script_previews = load_script_previews(
            loaded_skill,
            query_text=query_text,
            limit=4,
            candidate_paths=candidate_runnable_scripts,
        )
        doc_bundle = build_skill_doc_bundle(loaded_skill, request)
        file_inventory = _filter_file_inventory_for_free_shell(
            [item.to_dict() for item in loaded_skill.file_inventory],
            doc_bundle=doc_bundle,
            candidate_script_paths=candidate_runnable_scripts,
        )
        cli_history: list[dict[str, Any]] = []
        shell_hint_summary = _summarize_shell_hints_for_free_shell(shell_hints)
        compact_initial_context = _should_compact_initial_free_shell_context(
            loaded_skill=loaded_skill,
            request=request,
            shell_hints=shell_hints,
            file_inventory=file_inventory,
            doc_bundle=doc_bundle,
        )
        initial_file_inventory = (
            _compact_file_inventory_for_free_shell(file_inventory)
            if compact_initial_context
            else file_inventory
        )
        initial_doc_bundle = (
            compact_skill_doc_bundle(doc_bundle)
            if compact_initial_context
            else doc_bundle
        )
        initial_script_previews = (
            _compact_script_previews_for_free_shell(script_previews)
            if compact_initial_context
            else script_previews
        )
        prompt_attempts = [
            {
                "label": "full_context",
                "transport": "json",
                "max_tokens": FREE_SHELL_MAX_TOKENS,
                "shell_hints": shell_hint_summary,
                "file_inventory": initial_file_inventory,
                "doc_bundle": initial_doc_bundle,
                "script_previews": initial_script_previews,
                "cli_history": cli_history,
                "python_examples": python_examples,
            },
            {
                "label": "compact_context",
                "transport": "json",
                "max_tokens": FREE_SHELL_MAX_TOKENS,
                "shell_hints": shell_hint_summary,
                "file_inventory": _compact_file_inventory_for_free_shell(file_inventory),
                "doc_bundle": compact_skill_doc_bundle(doc_bundle),
                "script_previews": _compact_script_previews_for_free_shell(script_previews),
                "cli_history": cli_history,
                "python_examples": [],
            },
            {
                "label": "micro_context",
                "transport": "json",
                "max_tokens": FREE_SHELL_MICRO_MAX_TOKENS,
                "shell_hints": shell_hint_summary,
                "file_inventory": _micro_file_inventory_for_free_shell(file_inventory),
                "doc_bundle": _micro_doc_bundle_for_free_shell(doc_bundle),
                "script_previews": _micro_script_previews_for_free_shell(script_previews),
                "cli_history": cli_history,
                "python_examples": [],
            },
            {
                "label": "micro_context",
                "transport": "text_first",
                "max_tokens": FREE_SHELL_MICRO_MAX_TOKENS,
                "shell_hints": shell_hint_summary,
                "file_inventory": _micro_file_inventory_for_free_shell(file_inventory),
                "doc_bundle": _micro_doc_bundle_for_free_shell(doc_bundle),
                "script_previews": _micro_script_previews_for_free_shell(script_previews),
                "cli_history": cli_history,
                "python_examples": [],
            },
        ]
        payload: dict[str, Any] | None = None
        planning_errors: list[str] = []
        planner_attempts: list[dict[str, Any]] = []
        selected_attempt: dict[str, Any] | None = None
        for attempt in prompt_attempts:
            system_prompt, user_prompt = build_skill_free_shell_planner_prompt(
                skill_name=request.skill_name,
                goal=request.goal,
                user_query=request.user_query,
                runtime_target=request.runtime_target.to_dict(),
                constraints=request.constraints,
                effective_constraints=effective_constraints,
                shell_hints=attempt["shell_hints"],
                file_inventory=attempt["file_inventory"],
                doc_bundle=attempt["doc_bundle"],
                cli_history=attempt["cli_history"],
                script_previews=attempt["script_previews"],
                python_examples=attempt["python_examples"],
                conservative_plan=fallback_plan.to_dict(),
            )
            try:
                if attempt["transport"] == "text_first":
                    payload = await _complete_json_via_text_first(
                        self.llm_client,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=0.0,
                        max_tokens=int(attempt["max_tokens"]),
                    )
                else:
                    payload = await self.llm_client.complete_json(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=0.0,
                        max_tokens=int(attempt["max_tokens"]),
                    )
                selected_attempt = dict(attempt)
                planner_attempts.append(
                    {
                        "attempt_index": len(planner_attempts) + 1,
                        "label": str(attempt["label"]),
                        "transport": str(attempt["transport"]),
                        "max_tokens": int(attempt["max_tokens"]),
                        "success": True,
                        "error_summary": None,
                    }
                )
                shell_hints = {
                    **shell_hints,
                    "planner_context_mode": str(attempt["label"]),
                    "planner_transport": str(attempt["transport"]),
                    "planner_attempts": planner_attempts,
                }
                if compact_initial_context:
                    shell_hints["planner_full_context_compacted"] = True
                    shell_hints["planner_full_context_compaction_reason"] = (
                        "large_actionable_context"
                    )
                break
            except Exception as exc:
                error_summary = _truncate_text(str(exc), max_chars=1200)
                planning_errors.append(f"{attempt['label']}/{attempt['transport']}: {exc}")
                planner_attempts.append(
                    {
                        "attempt_index": len(planner_attempts) + 1,
                        "label": str(attempt["label"]),
                        "transport": str(attempt["transport"]),
                        "max_tokens": int(attempt["max_tokens"]),
                        "success": False,
                        "error_summary": error_summary,
                    }
                )
        if payload is None:
            last_attempt = prompt_attempts[-1] if prompt_attempts else {}
            return self._manual_required_plan(
                request=request,
                shell_hints={
                    **shell_hints,
                    "planner": "free_shell",
                    "planner_context_mode": str(last_attempt.get("label", "")),
                    "planner_transport": str(last_attempt.get("transport", "")),
                    "planner_attempts": planner_attempts,
                    "warnings": [f"Free-shell planning failed: {item}" for item in planning_errors],
                },
                failure_reason="llm_planning_failed",
                missing_fields=list(fallback_plan.missing_fields),
                rationale=(
                    "The free-shell planner hit a technical failure before it could produce a "
                    "runnable command plan."
                ),
                planner_error_summary=summarize_planner_error(*planning_errors),
                manual_required_kind="technical_blocked",
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
        (
            bootstrap_commands,
            required_tools,
            warnings,
            bootstrap_reason,
        ) = normalize_free_shell_runtime_requirements(
            runtime_target=request.runtime_target,
            command=command,
            entrypoint=generated_entrypoint,
            generated_files=generated_files,
            bootstrap_commands=bootstrap_commands,
            required_tools=required_tools,
            warnings=warnings,
            bootstrap_reason=bootstrap_reason,
        )
        hints = {
            **shell_hints,
            "planner": "free_shell",
            "required_tools": required_tools,
            "warnings": warnings,
            "doc_bundle": doc_bundle,
            "script_previews": script_previews,
            "python_examples": python_examples,
            "promoted_inline_python_to_generated_script": promoted_inline_python,
        }
        if selected_attempt is not None:
            hints["planner_context_mode"] = str(selected_attempt.get("label", ""))
            hints["planner_transport"] = str(selected_attempt.get("transport", ""))
        if planner_attempts:
            hints["planner_attempts"] = planner_attempts

        if raw_mode == "manual_required":
            return self._manual_required_plan(
                request=request,
                shell_hints=hints,
                failure_reason=failure_reason or "manual_command_required",
                missing_fields=missing_fields,
                rationale=rationale,
                planner_error_summary=summarize_planner_error(
                    str(failure_reason or "").strip(),
                    str(rationale or "").strip(),
                    *warnings,
                ),
            )
        if command is None:
            return self._manual_required_plan(
                request=request,
                shell_hints=hints,
                failure_reason="llm_planning_failed",
                missing_fields=missing_fields,
                rationale=(
                    "The free-shell planner returned an incomplete plan without a runnable "
                    "command or generated entrypoint."
                ),
                planner_error_summary=summarize_planner_error(
                    "Planner returned no runnable command or generated entrypoint.",
                    *warnings,
                ),
                manual_required_kind="technical_blocked",
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
                manual_required_kind="technical_blocked",
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
        planner_error_summary: str | None = None,
        manual_required_kind: str | None = None,
    ) -> SkillCommandPlan:
        resolved_manual_required_kind = manual_required_kind or classify_manual_required_kind(
            failure_reason
        )
        resolved_planner_error_summary = planner_error_summary or summarize_planner_error(
            str(failure_reason or "").strip(),
            str(rationale or "").strip(),
            *(str(item) for item in shell_hints.get("warnings", []) if isinstance(item, str)),
        )
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
            hints={
                **dict(shell_hints),
                "manual_required_kind": resolved_manual_required_kind,
                "planner_error_summary": resolved_planner_error_summary,
            },
        )
