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


@dataclass(frozen=True)
class PathExplainerPromptTemplate:
    template_id: str
    description: str
    system_prompt: str
    explanation_guidance: str


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
DEFAULT_PATH_EXPLAINER_TEMPLATE_ID = "path_base_v1"
_PATH_EXPLAINER_TEMPLATE_ID_ALIASES = {
    "default": DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
    "stable": DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
    "latest": DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
}
_PATH_EXPLAINER_INTENT_FALLBACKS = {
    "causal_explanation": "intent_causal_v1",
    "containment_trace": "intent_containment_v1",
    "prerequisite_trace": "intent_prerequisite_v1",
    "provenance_trace": "intent_provenance_v1",
}
_PATH_EXPLAINER_PROMPT_TEMPLATES: dict[str, PathExplainerPromptTemplate] = {
    "path_base_v1": PathExplainerPromptTemplate(
        template_id="path_base_v1",
        description="General-purpose path explanation template.",
        system_prompt=(
            "You are a path explanation module. "
            "Explain graph paths using only the provided paths and evidence. "
            "Follow the resolved output contract and guardrails. "
            "If evidence is weak or partial, say so explicitly. "
            "Do not invent new relations."
        ),
        explanation_guidance=(
            "Write a concise explanation grounded in the selected path and evidence. "
            "Prefer explicit mention of what the path supports versus what remains uncertain."
        ),
    ),
    "intent_causal_v1": PathExplainerPromptTemplate(
        template_id="intent_causal_v1",
        description="General causal explanation template.",
        system_prompt=(
            "You are a path explanation module for causal reasoning. "
            "Explain graph paths using only the provided paths and evidence. "
            "Describe the causal chain carefully, and separate supported effects from tentative ones. "
            "Do not invent new relations."
        ),
        explanation_guidance=(
            "Prefer driver -> mechanism -> outcome wording when supported. "
            "If the evidence does not establish the full chain, mark the unsupported step explicitly."
        ),
    ),
    "intent_containment_v1": PathExplainerPromptTemplate(
        template_id="intent_containment_v1",
        description="Hierarchy and membership explanation template.",
        system_prompt=(
            "You are a path explanation module for hierarchy and membership queries. "
            "Explain containment or belonging relations using only the provided paths and evidence. "
            "Avoid causal wording unless the evidence explicitly supports causality."
        ),
        explanation_guidance=(
            "Explain the hierarchy or membership structure directly. "
            "Do not recast containment relations as causal influence."
        ),
    ),
    "intent_prerequisite_v1": PathExplainerPromptTemplate(
        template_id="intent_prerequisite_v1",
        description="Dependency and prerequisite explanation template.",
        system_prompt=(
            "You are a path explanation module for dependency queries. "
            "Explain prerequisite or dependency relations using only the provided paths and evidence. "
            "Be explicit about order and dependency direction."
        ),
        explanation_guidance=(
            "Prefer ordered dependency wording and identify which step depends on which other step."
        ),
    ),
    "intent_provenance_v1": PathExplainerPromptTemplate(
        template_id="intent_provenance_v1",
        description="Source-chain and provenance explanation template.",
        system_prompt=(
            "You are a path explanation module for provenance and traceability queries. "
            "Explain source chains using only the provided paths and evidence. "
            "Prefer document-backed or source-backed claims over inferred claims."
        ),
        explanation_guidance=(
            "Describe the source chain, version chain, or responsibility chain explicitly when the evidence supports it."
        ),
    ),
    "economy_causal_v1": PathExplainerPromptTemplate(
        template_id="economy_causal_v1",
        description="Economy-domain causal path explanation template.",
        system_prompt=(
            "You are a path explanation module for economy and finance reasoning. "
            "Explain economic or market driver paths using only the provided graph paths and evidence. "
            "Avoid overstating quantitative impact when the evidence is qualitative only."
        ),
        explanation_guidance=(
            "Prefer explicit driver -> transmission channel -> metric or company outcome wording. "
            "Separate structural support from numerical impact claims."
        ),
    ),
    "economy_metric_driver_v1": PathExplainerPromptTemplate(
        template_id="economy_metric_driver_v1",
        description="Economy-domain metric-driver explanation template.",
        system_prompt=(
            "You are a path explanation module for metric-driver queries in economy and finance. "
            "Explain what drives the target metric using only the provided graph paths and evidence. "
            "Do not imply precise magnitude without explicit evidence."
        ),
        explanation_guidance=(
            "Focus on which factor drives the metric, through what channel, and what evidence supports the linkage."
        ),
    ),
    "economy_membership_v1": PathExplainerPromptTemplate(
        template_id="economy_membership_v1",
        description="Economy-domain industry membership explanation template.",
        system_prompt=(
            "You are a path explanation module for economy-domain membership and industry-position queries. "
            "Explain industry, sector, or chain-position relations using only the provided paths and evidence."
        ),
        explanation_guidance=(
            "Prefer concise industry-membership wording and avoid causal interpretation for structural relations."
        ),
    ),
    "economy_attribute_v1": PathExplainerPromptTemplate(
        template_id="economy_attribute_v1",
        description="Economy-domain attribute mapping explanation template.",
        system_prompt=(
            "You are a path explanation module for economy-domain attribute queries. "
            "Explain company, institution, or market attributes using only the provided paths and evidence."
        ),
        explanation_guidance=(
            "Focus on the mapped attribute and the supporting path, not on speculative implications."
        ),
    ),
}


def list_route_judge_prompt_versions() -> list[str]:
    return sorted(_ROUTE_JUDGE_PROMPT_TEMPLATES)


def list_path_explainer_prompt_templates() -> list[str]:
    return sorted(_PATH_EXPLAINER_PROMPT_TEMPLATES)


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


def resolve_path_explainer_prompt_template(
    template_id: str | None,
    *,
    intent_family: str | None = None,
) -> PathExplainerPromptTemplate:
    normalized = (template_id or DEFAULT_PATH_EXPLAINER_TEMPLATE_ID).strip().lower()
    normalized = _PATH_EXPLAINER_TEMPLATE_ID_ALIASES.get(normalized, normalized)
    if normalized in _PATH_EXPLAINER_PROMPT_TEMPLATES:
        return _PATH_EXPLAINER_PROMPT_TEMPLATES[normalized]

    fallback_template_id = _PATH_EXPLAINER_INTENT_FALLBACKS.get(
        (intent_family or "").strip()
    )
    if fallback_template_id and fallback_template_id in _PATH_EXPLAINER_PROMPT_TEMPLATES:
        logger.warning(
            "Unknown path explainer template '%s'; falling back to intent template %s.",
            template_id,
            fallback_template_id,
        )
        return _PATH_EXPLAINER_PROMPT_TEMPLATES[fallback_template_id]

    if normalized not in _PATH_EXPLAINER_PROMPT_TEMPLATES:
        logger.warning(
            "Unknown path explainer template '%s'; falling back to %s.",
            template_id,
            DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
        )
    return _PATH_EXPLAINER_PROMPT_TEMPLATES[DEFAULT_PATH_EXPLAINER_TEMPLATE_ID]


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
    explanation_profile: dict[str, Any] | None = None,
    intent_family: str | None = None,
    template_id: str | None = None,
    scenario_id: str | None = None,
    scenario_override: dict[str, Any] | None = None,
    evidence_policy: dict[str, Any] | None = None,
    output_contract: dict[str, Any] | None = None,
    guardrails: dict[str, Any] | None = None,
) -> tuple[str, str]:
    template = resolve_path_explainer_prompt_template(
        template_id,
        intent_family=intent_family,
    )
    system_prompt = template.system_prompt
    user_prompt = (
        "User query:\n"
        f"{query}\n\n"
        "Resolved intent family:\n"
        f"{intent_family or 'unknown'}\n\n"
        "Resolved scenario id:\n"
        f"{scenario_id or 'none'}\n\n"
        "Requested template id:\n"
        f"{template_id or 'default'}\n\n"
        "Resolved prompt template:\n"
        f"{template.template_id}\n\n"
        "Prompt description:\n"
        f"{template.description}\n\n"
        "Template guidance:\n"
        f"{template.explanation_guidance}\n\n"
        "Resolved scenario override:\n"
        f"{json.dumps(scenario_override or {}, ensure_ascii=False, indent=2)}\n\n"
        "Resolved evidence policy:\n"
        f"{json.dumps(evidence_policy or {}, ensure_ascii=False, indent=2)}\n\n"
        "Resolved output contract:\n"
        f"{json.dumps(output_contract or {}, ensure_ascii=False, indent=2)}\n\n"
        "Resolved guardrails:\n"
        f"{json.dumps(guardrails or {}, ensure_ascii=False, indent=2)}\n\n"
        "Explanation profile:\n"
        f"{json.dumps(explanation_profile or {}, ensure_ascii=False, indent=2)}\n\n"
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
