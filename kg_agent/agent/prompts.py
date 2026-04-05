from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteJudgePromptTemplate:
    version: str
    description: str
    system_prompt: str
    planner_guidance: str


DEFAULT_ROUTE_JUDGE_PROMPT_VERSION = "v1"
_ROUTE_JUDGE_PROMPT_VERSION_ALIASES = {
    "default": DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    "stable": DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    "latest": "v2",
}
_ROUTE_JUDGE_PROMPT_TEMPLATES: dict[str, RouteJudgePromptTemplate] = {
    "v1": RouteJudgePromptTemplate(
        version="v1",
        description="Original concise route-refinement prompt.",
        system_prompt=(
            "You are the route judge inside a tool-using agent. "
            "Decide whether tools are needed, which tools to call, and in what order. "
            "Return strict JSON only."
        ),
        planner_guidance=(
            "Refine the current rule-based plan when needed, but do not invent tools outside available_tools."
        ),
    ),
    "v2": RouteJudgePromptTemplate(
        version="v2",
        description="More conservative route-refinement prompt with explicit refinement policy.",
        system_prompt=(
            "You are the route judge inside a tool-using agent. "
            "You are refining a rule-based plan, not replacing it from scratch. "
            "Prefer minimal edits, keep the tool sequence short, and return strict JSON only."
        ),
        planner_guidance=(
            "Refinement policy:\n"
            "1. Keep the current rule-based plan unless the query or context clearly requires a change.\n"
            "2. Use memory tools only for contextual follow-ups.\n"
            "3. Use web_search for direct URLs, realtime/freshness needs, or correction-driven refresh.\n"
            "4. Preserve tool order unless there is a concrete reason to reorder it.\n"
            "5. Never invent tools outside available_tools."
        ),
    ),
}


def list_route_judge_prompt_versions() -> list[str]:
    return sorted(_ROUTE_JUDGE_PROMPT_TEMPLATES)


def resolve_route_judge_prompt_version(prompt_version: str | None) -> str:
    normalized = (prompt_version or DEFAULT_ROUTE_JUDGE_PROMPT_VERSION).strip().lower()
    normalized = _ROUTE_JUDGE_PROMPT_VERSION_ALIASES.get(normalized, normalized)
    if normalized in _ROUTE_JUDGE_PROMPT_TEMPLATES:
        return normalized
    logger.warning(
        "Unknown route judge prompt version '%s'; falling back to %s.",
        prompt_version,
        DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    )
    return DEFAULT_ROUTE_JUDGE_PROMPT_VERSION


def build_route_judge_prompt(
    *,
    query: str,
    session_context: dict[str, Any] | None,
    available_tools: list[str],
    current_plan: dict[str, Any],
    prompt_version: str | None = None,
) -> tuple[str, str]:
    resolved_version = resolve_route_judge_prompt_version(prompt_version)
    template = _ROUTE_JUDGE_PROMPT_TEMPLATES[resolved_version]
    system_prompt = template.system_prompt
    user_prompt = (
        f"Prompt version: {template.version}\n"
        f"Prompt description: {template.description}\n\n"
        "User query:\n"
        f"{query}\n\n"
        "Recent session context:\n"
        f"{json.dumps(session_context or {}, ensure_ascii=False, indent=2)}\n\n"
        "Available tools:\n"
        f"{json.dumps(available_tools, ensure_ascii=False)}\n\n"
        "Current rule-based plan:\n"
        f"{json.dumps(current_plan, ensure_ascii=False, indent=2)}\n\n"
        f"{template.planner_guidance}\n\n"
        "Return a JSON object with these fields:\n"
        "{"
        '"need_tools": bool, '
        '"need_memory": bool, '
        '"need_web_search": bool, '
        '"need_path_explanation": bool, '
        '"strategy": str, '
        '"tool_sequence": [{"tool": str, "args": dict, "optional": bool}], '
        '"reason": str, '
        '"max_iterations": int'
        "}\n"
        "Keep JSON valid. Do not include markdown fences."
    )
    return system_prompt, user_prompt


def build_path_explainer_prompt(
    *,
    query: str,
    graph_paths: list[dict[str, Any]],
    evidence_chunks: list[str],
    domain_schema: dict[str, Any] | None = None,
) -> tuple[str, str]:
    system_prompt = (
        "You are a path explanation module. "
        "Explain graph paths using only the provided paths and evidence. "
        "If evidence is weak, say so. Do not invent new relations."
    )
    user_prompt = (
        "User query:\n"
        f"{query}\n\n"
        "Domain schema:\n"
        f"{json.dumps(domain_schema or {}, ensure_ascii=False, indent=2)}\n\n"
        "Candidate graph paths:\n"
        f"{json.dumps(graph_paths, ensure_ascii=False, indent=2)}\n\n"
        "Evidence chunks:\n"
        f"{json.dumps(evidence_chunks, ensure_ascii=False, indent=2)}\n\n"
        'Return a JSON object with fields: {"final_explanation": str, "uncertainty": str}.'
    )
    return system_prompt, user_prompt


def build_final_answer_prompt(
    *,
    query: str,
    route: dict[str, Any],
    tool_results: list[dict[str, Any]],
    path_explanation: dict[str, Any] | None,
    conversation_history: list[dict[str, str]] | None,
) -> tuple[str, str]:
    system_prompt = (
        "You are the final answering module of a knowledge-graph-enhanced agent. "
        "Answer in the same language as the user query. "
        "Use tool results and path explanation when available. "
        "Be explicit about uncertainty when tools failed or evidence is incomplete."
    )
    user_prompt = (
        "User query:\n"
        f"{query}\n\n"
        "Route decision:\n"
        f"{json.dumps(route, ensure_ascii=False, indent=2)}\n\n"
        "Recent conversation history:\n"
        f"{json.dumps(conversation_history or [], ensure_ascii=False, indent=2)}\n\n"
        "Tool results:\n"
        f"{json.dumps(tool_results, ensure_ascii=False, indent=2)}\n\n"
        "Path explanation:\n"
        f"{json.dumps(path_explanation or {}, ensure_ascii=False, indent=2)}\n\n"
        "Write a clear final answer for the user."
    )
    return system_prompt, user_prompt
