from __future__ import annotations

import json
from typing import Any


def build_route_judge_prompt(
    *,
    query: str,
    session_context: dict[str, Any] | None,
    available_tools: list[str],
    current_plan: dict[str, Any],
) -> tuple[str, str]:
    system_prompt = (
        "You are the route judge inside a tool-using agent. "
        "Decide whether tools are needed, which tools to call, and in what order. "
        "Return strict JSON only."
    )
    user_prompt = (
        "User query:\n"
        f"{query}\n\n"
        "Recent session context:\n"
        f"{json.dumps(session_context or {}, ensure_ascii=False, indent=2)}\n\n"
        "Available tools:\n"
        f"{json.dumps(available_tools, ensure_ascii=False)}\n\n"
        "Current rule-based plan:\n"
        f"{json.dumps(current_plan, ensure_ascii=False, indent=2)}\n\n"
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
        "Do not invent tools outside available_tools."
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
