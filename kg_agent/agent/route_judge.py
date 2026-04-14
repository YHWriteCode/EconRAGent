from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from kg_agent.agent.prompts import (
    DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    build_route_judge_prompt,
    resolve_route_judge_prompt_version,
)
from kg_agent.skills import SkillPlan


FOLLOWUP_PATTERN = re.compile(
    r"("
    r"\u4e4b\u524d\u8bf4\u8fc7|"
    r"\u4e0a\u6b21\u63d0\u5230|"
    r"\u7ee7\u7eed\u521a\u624d|"
    r"\u7ee7\u7eed\u4e0a\u4e2a|"
    r"\u521a\u624d\u63d0\u5230|"
    r"earlier|previously|continue"
    r")",
    re.IGNORECASE,
)
REALTIME_PATTERN = re.compile(
    r"("
    r"\u6700\u65b0|"
    r"\u4eca\u65e5|"
    r"\u4eca\u5929|"
    r"\u5b9e\u65f6|"
    r"\u8fd1\u671f|"
    r"\u73b0\u5728|"
    r"latest|today|current|real.?time|recent"
    r")",
    re.IGNORECASE,
)
ENTITY_PATTERN = re.compile(
    r"("
    r"\u662f\u4ec0\u4e48|"
    r"\u505a\u4ec0\u4e48|"
    r"\u5c5e\u4e8e\u4ec0\u4e48|"
    r"\u662f\u4ec0\u4e48\u516c\u53f8|"
    r"what is|who is|what does|belongs to"
    r")",
    re.IGNORECASE,
)
RELATION_PATTERN = re.compile(
    r"("
    r"\u4e3a\u4ec0\u4e48|"
    r"\u5f71\u54cd|"
    r"\u4f20\u5bfc|"
    r"\u548c\u8c01\u6709\u5173|"
    r"\u5173\u7cfb|"
    r"\u5173\u8054|"
    r"why|impact|affect|relation|related"
    r")",
    re.IGNORECASE,
)
SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN = re.compile(
    r"("
    r"\u6536\u76ca\u7387|"
    r"\u56de\u6d4b|"
    r"\u590f\u666e|"
    r"\u6700\u5927\u56de\u64a4|"
    r"\u7b56\u7565\u8868\u73b0|"
    r"backtest|sharpe|drawdown|strategy"
    r")",
    re.IGNORECASE,
)
SKILL_REQUEST_PATTERN = re.compile(
    r"("
    r"\u6280\u80fd|"
    r"\u6280\u80fd\u5305|"
    r"\u6280\u80fd\u5de5\u4f5c\u6d41|"
    r"\u5de5\u4f5c\u6d41|"
    r"\u7535\u5b50\u8868\u683c|"
    r"\u8868\u683c\u6587\u4ef6|"
    r"agent skill|agent skills|skill|skills|workflow|spreadsheet|xlsx|xlsm|csv|tsv"
    r")",
    re.IGNORECASE,
)
SIMPLE_PATTERN = re.compile(
    r"^\s*(hi|hello|\u4f60\u597d|\u55e8|\u8c22\u8c22|thanks|thank you)\s*[!?,.\u3002\uff1f\uff01]*\s*$",
    re.IGNORECASE,
)
INGEST_PATTERN = re.compile(
    r"("
    r"\u6536\u5f55|"
    r"\u5165\u5e93|"
    r"\u5199\u5165|"
    r"\u5bfc\u5165|"
    r"\u6dfb\u52a0\u5230\u77e5\u8bc6|"
    r"\u5b58\u5165|"
    r"\u5f55\u5165|"
    r"ingest|import.*(into|to).*(kg|graph|knowledge)|"
    r"add.*(to|into).*(kg|graph|knowledge)|store.*(into|to)"
    r")",
    re.IGNORECASE,
)
CORRECTION_PATTERN = re.compile(
    r"("
    r"\u4e0d\u5bf9|"
    r"\u8fc7\u65f6|"
    r"\u5df2\u7ecf\u53d8\u4e86|"
    r"\u6709\u8bef|"
    r"\u4fee\u6b63\u4e00\u4e0b|"
    r"\u7ea0\u6b63\u4e00\u4e0b|"
    r"\u66f4\u65b0\u4e00\u4e0b|"
    r"\u4e0d\u662f\u8fd9\u6837|"
    r"that'?s wrong|outdated|changed|incorrect|update this"
    r")",
    re.IGNORECASE,
)
CORRECTION_PHRASE_PATTERN = re.compile(
    r"("
    r"\u8fd9\u4e2a\u6570\u636e|"
    r"\u8fd9\u4e2a\u7ed3\u8bba|"
    r"\u8fd9\u4e2a\u8bf4\u6cd5|"
    r"\u8fd9\u6761\u4fe1\u606f|"
    r"\u4e0d\u5bf9|"
    r"\u8fc7\u65f6\u4e86|"
    r"\u5df2\u7ecf\u53d8\u4e86|"
    r"\u6709\u8bef|"
    r"\u4fee\u6b63\u4e00\u4e0b|"
    r"\u7ea0\u6b63\u4e00\u4e0b|"
    r"\u66f4\u65b0\u4e00\u4e0b|"
    r"\u4e0d\u662f\u8fd9\u6837|"
    r"that'?s wrong|outdated|changed|incorrect|update this"
    r")",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s)>\"']+", re.IGNORECASE)
FILE_PATH_PATTERN = re.compile(
    r'(?:"(?P<dq>[^"\r\n]+\.(?:xlsx|xlsm|xls|csv|tsv))"|'
    r"'(?P<sq>[^'\r\n]+\.(?:xlsx|xlsm|xls|csv|tsv))'|"
    r"(?P<bare>(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|/)?[^\s\"'<>|]+\.(?:xlsx|xlsm|xls|csv|tsv)))",
    re.IGNORECASE,
)
OUTPUT_PATH_PATTERN = re.compile(
    r'(?:(?:save(?: it)? (?:as|to)|write(?: it)? to|output(?: it)? (?:as|to)|into)\s+)'
    r'(?:"(?P<dq>[^"\r\n]+\.[A-Za-z0-9]{1,8})"|'
    r"'(?P<sq>[^'\r\n]+\.[A-Za-z0-9]{1,8})'|"
    r"(?P<bare>(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|/)?[^\s\"'<>|]+\.[A-Za-z0-9]{1,8}))",
    re.IGNORECASE,
)
CLI_ARG_PATTERN = re.compile(
    r"(?<!\S)(?P<flag>--?[A-Za-z][A-Za-z0-9_-]*)(?:[=\s]+(?P<value>\"[^\"]+\"|'[^']+'|[^\s,;]+))?",
    re.IGNORECASE,
)
FORMAT_HINT_PATTERN = re.compile(
    r"\b(?:as|format(?:ted)? as|output format(?: is)?|in)\s+"
    r"(?P<format>markdown|md|json|csv|xlsx|xlsm|xls|tsv|html|pdf)"
    r"(?:\s+format)?\b",
    re.IGNORECASE,
)
MODE_HINT_PATTERN = re.compile(
    r"\b(?P<mode>strict|safe|fast|incremental|append|overwrite)\s+mode\b|"
    r"\bmode\s+(?P<named>[a-z][a-z0-9_-]+)\b",
    re.IGNORECASE,
)
DRY_RUN_PATTERN = re.compile(
    r"(dry[- ]run|plan only|without execution|don'?t execute|do not execute)",
    re.IGNORECASE,
)
FREE_SHELL_PATTERN = re.compile(
    r"(\u81ea\u7531\s*shell|\u81ea\u7531\u6a21\u5f0f|free\s+shell|shell\s+agent)",
    re.IGNORECASE,
)
CONSERVATIVE_SHELL_PATTERN = re.compile(
    r"(\u4fdd\u5b88\u6a21\u5f0f|\u663e\u5f0f\u6307\u4ee4|conservative\s+mode|safe\s+shell)",
    re.IGNORECASE,
)
FORMULA_RECALC_PATTERN = re.compile(
    r"("
    r"recalc|recalculate|recalculation|"
    r"formula error|formula errors|"
    r"#ref!|#div/0!|#value!|#name\?|"
    r"\u91cd\u7b97|\u91cd\u65b0\u8ba1\u7b97|\u516c\u5f0f\u91cd\u7b97|\u516c\u5f0f\u9519\u8bef"
    r")",
    re.IGNORECASE,
)
PRESERVE_FORMULA_PATTERN = re.compile(
    r"("
    r"keep formulas intact|preserve formulas|retain formulas|"
    r"\u4fdd\u7559\u516c\u5f0f|\u4e0d\u8981\u7834\u574f\u516c\u5f0f"
    r")",
    re.IGNORECASE,
)
KG_RETRIEVAL_TOOLS = {
    "kg_hybrid_search",
    "kg_naive_search",
    "graph_entity_lookup",
    "graph_relation_trace",
}
SKILL_INFRASTRUCTURE_TOOLS = {
    "list_skills",
    "read_skill",
    "read_skill_docs",
    "read_skill_file",
    "run_skill_task",
    "execute_skill_script",
}
MATCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "for",
    "from",
    "help",
    "into",
    "need",
    "query",
    "run",
    "show",
    "that",
    "the",
    "this",
    "tool",
    "use",
    "using",
    "with",
}


@dataclass
class ToolCallPlan:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    input_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class RouteDecision:
    need_tools: bool
    need_memory: bool
    need_web_search: bool
    need_path_explanation: bool
    strategy: str
    tool_sequence: list[ToolCallPlan]
    reason: str
    max_iterations: int = 3
    skill_plan: SkillPlan | None = None


class RouteJudge:
    def __init__(
        self,
        *,
        llm_client=None,
        default_max_iterations: int = 3,
        prompt_version: str = DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    ):
        self.llm_client = llm_client
        self.default_max_iterations = default_max_iterations
        self.prompt_version = resolve_route_judge_prompt_version(prompt_version)

    async def plan(
        self,
        query: str,
        session_context: dict[str, Any] | None,
        user_profile: dict[str, Any] | None,
        available_capabilities: list[Any] | None = None,
        available_capability_catalog: list[dict[str, Any]] | None = None,
        available_tools: list[str] | None = None,
        available_skills: list[dict[str, Any]] | None = None,
    ) -> RouteDecision:
        capability_catalog = self._resolve_capability_catalog(
            available_capabilities=available_capabilities,
            available_capability_catalog=available_capability_catalog,
            available_tools=available_tools,
        )
        skill_catalog = self._resolve_skill_catalog(available_skills)
        resolved_capabilities = [
            item["name"] for item in capability_catalog if isinstance(item.get("name"), str)
        ]
        base_decision = self._rule_based_fallback(
            query=query,
            session_context=session_context,
            available_tools=resolved_capabilities,
            capability_catalog=capability_catalog,
            skill_catalog=skill_catalog,
        )
        refined = await self._maybe_refine_with_llm(
            query=query,
            session_context=session_context,
            user_profile=user_profile,
            available_tools=resolved_capabilities,
            capability_catalog=capability_catalog,
            skill_catalog=skill_catalog,
            base_decision=base_decision,
        )
        return refined or base_decision

    def _rule_based_fallback(
        self,
        *,
        query: str,
        session_context: dict[str, Any] | None,
        available_tools: list[str],
        capability_catalog: list[dict[str, Any]],
        skill_catalog: list[dict[str, Any]],
    ) -> RouteDecision:
        normalized_query = (query or "").strip()
        history = (
            session_context.get("history", []) if isinstance(session_context, dict) else []
        )
        recent_tool_calls = (
            session_context.get("recent_tool_calls", [])
            if isinstance(session_context, dict)
            else []
        )
        has_history = bool(history)
        has_cross_session_tool = "cross_session_search" in available_tools
        matched_skill = self._select_matching_skill(
            query=normalized_query,
            skill_catalog=skill_catalog,
        )
        matched_external_capability = self._select_matching_external_capability(
            query=normalized_query,
            capability_catalog=capability_catalog,
        )

        is_simple = bool(SIMPLE_PATTERN.search(normalized_query))
        is_followup = bool(FOLLOWUP_PATTERN.search(normalized_query)) and (
            has_history or has_cross_session_tool
        )
        needs_realtime = bool(REALTIME_PATTERN.search(normalized_query))
        needs_specialized_external_capability = bool(
            SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN.search(normalized_query)
        )
        requests_skill_workflow = bool(SKILL_REQUEST_PATTERN.search(normalized_query))
        is_relation = bool(RELATION_PATTERN.search(normalized_query))
        is_entity = bool(ENTITY_PATTERN.search(normalized_query))
        is_ingest = bool(INGEST_PATTERN.search(normalized_query))
        is_correction = bool(CORRECTION_PATTERN.search(normalized_query)) and (
            self._last_turn_used_kg_tools(recent_tool_calls)
        )
        direct_urls = URL_PATTERN.findall(normalized_query)

        sequence: list[ToolCallPlan] = []
        need_memory = False
        need_path_explanation = False
        strategy = "simple_answer_no_tool"
        reason = "The query can be handled without external tools."

        if is_simple and not any(
            [
                needs_specialized_external_capability,
                needs_realtime,
                is_relation,
                is_entity,
                is_ingest,
                is_correction,
                matched_skill is not None,
            ]
        ):
            return RouteDecision(
                need_tools=False,
                need_memory=False,
                need_web_search=False,
                need_path_explanation=False,
                strategy=strategy,
                tool_sequence=[],
                reason=reason,
                max_iterations=1,
            )

        if is_followup and "memory_search" in available_tools and has_history and not is_correction:
            sequence.append(ToolCallPlan(tool="memory_search", args={"limit": 4}))
            need_memory = True
        elif is_followup and has_cross_session_tool and not is_correction:
            sequence.append(ToolCallPlan(tool="cross_session_search", args={"limit": 4}))
            need_memory = True

        if matched_skill is not None and self._should_route_to_skill(
            query=normalized_query,
            matched_skill=matched_skill,
            requests_skill_workflow=requests_skill_workflow,
            needs_specialized_external_capability=needs_specialized_external_capability,
        ):
            skill_constraints = self._build_skill_constraints(
                query=normalized_query,
                matched_skill=matched_skill,
            )
            skill_plan = SkillPlan(
                skill_name=matched_skill["name"],
                goal=self._build_skill_goal(normalized_query, matched_skill),
                reason=(
                    f"Matched local skill '{matched_skill['name']}' from the skill catalog."
                ),
                constraints=skill_constraints,
            )
            return RouteDecision(
                need_tools=bool(sequence),
                need_memory=need_memory,
                need_web_search=False,
                need_path_explanation=False,
                strategy="memory_first_then_skill" if need_memory else "skill_request",
                tool_sequence=self._normalize_tool_sequence(sequence, available_tools),
                reason=skill_plan.reason,
                max_iterations=max(1, self.default_max_iterations),
                skill_plan=skill_plan,
            )

        if needs_specialized_external_capability and matched_external_capability is not None:
            sequence.append(ToolCallPlan(tool=matched_external_capability["name"]))
            return RouteDecision(
                need_tools=True,
                need_memory=need_memory,
                need_web_search=False,
                need_path_explanation=False,
                strategy=(
                    "memory_first_then_external_capability"
                    if need_memory
                    else "external_capability_request"
                ),
                tool_sequence=self._normalize_tool_sequence(sequence, available_tools),
                reason=(
                    f"Matched external capability '{matched_external_capability['name']}' "
                    "for this specialized request."
                ),
                max_iterations=1,
            )

        if needs_specialized_external_capability:
            return RouteDecision(
                need_tools=False,
                need_memory=False,
                need_web_search=False,
                need_path_explanation=False,
                strategy="specialized_external_capability",
                tool_sequence=[],
                reason=(
                    "This request needs a specialized skill or external capability rather than "
                    "the built-in knowledge-graph or web tools."
                ),
                max_iterations=1,
            )

        if is_correction and "web_search" in available_tools:
            strategy = "correction_and_refresh"
            sequence.append(
                ToolCallPlan(
                    tool="web_search",
                    args={
                        "search_query": self._build_correction_search_query(
                            normalized_query,
                            history,
                        )
                    },
                )
            )
            if "kg_ingest" in available_tools:
                sequence.append(
                    ToolCallPlan(
                        tool="kg_ingest",
                        input_bindings={
                            "content": {
                                "from": "web_search",
                                "transform": "web_pages_markdown",
                            },
                            "source": {
                                "from": "web_search",
                                "transform": "web_pages_sources",
                            },
                        },
                    )
                )
            reason = (
                "The user is correcting prior graph-backed information, so refresh from the web first."
            )
        elif is_ingest and "kg_ingest" in available_tools:
            strategy = "kg_ingest_request"
            if "web_search" in available_tools:
                sequence.append(
                    ToolCallPlan(
                        tool="web_search",
                        args={"urls": direct_urls[:3]} if direct_urls else {},
                    )
                )
                sequence.append(
                    ToolCallPlan(
                        tool="kg_ingest",
                        input_bindings={
                            "content": {
                                "from": "web_search",
                                "transform": "web_pages_markdown",
                            },
                            "source": {
                                "from": "web_search",
                                "transform": "web_pages_sources",
                            },
                        },
                    )
                )
            else:
                sequence.append(ToolCallPlan(tool="kg_ingest"))
            reason = "The user wants to ingest content into the knowledge graph."
        elif direct_urls and "web_search" in available_tools:
            strategy = "direct_url_crawl"
            sequence.append(ToolCallPlan(tool="web_search", args={"urls": direct_urls[:3]}))
            reason = "The query includes direct URLs, so the crawler should fetch those pages first."
        elif matched_external_capability is not None and self._should_route_to_external_capability(
            query=normalized_query,
            matched_capability=matched_external_capability,
        ):
            strategy = (
                "memory_first_then_external_capability"
                if need_memory
                else "external_capability_request"
            )
            sequence.append(ToolCallPlan(tool=matched_external_capability["name"]))
            reason = (
                f"Matched external capability '{matched_external_capability['name']}' "
                "from the capability catalog."
            )
        elif needs_realtime:
            strategy, realtime_sequence, need_path_explanation, reason = (
                self._build_realtime_route(
                    is_relation=is_relation,
                    available_tools=available_tools,
                )
            )
            sequence.extend(realtime_sequence)
        elif is_relation:
            strategy = "kg_hybrid_first_then_graph_trace"
            if "kg_hybrid_search" in available_tools:
                sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
            if "graph_relation_trace" in available_tools:
                sequence.append(ToolCallPlan(tool="graph_relation_trace"))
                need_path_explanation = True
            reason = (
                "The query asks for relationships or impact chains, so retrieval and graph tracing are required."
            )
        elif is_entity:
            if "graph_entity_lookup" in available_tools:
                strategy = "graph_entity_lookup_first"
                sequence.append(ToolCallPlan(tool="graph_entity_lookup"))
                if "kg_hybrid_search" in available_tools:
                    sequence.append(ToolCallPlan(tool="kg_hybrid_search", optional=True))
                reason = (
                    "The query asks for an entity definition, so direct graph lookup is preferred."
                )
            elif "kg_hybrid_search" in available_tools:
                strategy = "kg_hybrid_entity_fallback"
                sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
                reason = "Graph entity lookup is unavailable, so hybrid retrieval is used."
        else:
            strategy = "factual_qa"
            if "kg_hybrid_search" in available_tools:
                sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
                reason = "Hybrid retrieval is the default strategy for factual knowledge questions."

        if need_memory and strategy == "factual_qa":
            strategy = "memory_first_then_hybrid"
            reason = "The query continues prior context, so memory is checked before hybrid retrieval."
        elif need_memory and strategy == "simple_answer_no_tool":
            strategy = "cross_session_memory_first"
            reason = "The query continues prior context and needs memory from earlier sessions."
        elif need_memory and strategy == "graph_entity_lookup_first":
            strategy = "memory_first_then_entity_lookup"
        elif need_memory and strategy == "kg_hybrid_first_then_graph_trace":
            strategy = "memory_first_then_relation_trace"
            reason = "The query depends on prior context and relationship reasoning."

        sequence = self._normalize_tool_sequence(sequence, available_tools)
        need_web_search = any(item.tool == "web_search" for item in sequence)
        return RouteDecision(
            need_tools=bool(sequence),
            need_memory=need_memory,
            need_web_search=need_web_search,
            need_path_explanation=need_path_explanation
            and any(item.tool == "graph_relation_trace" for item in sequence),
            strategy=strategy,
            tool_sequence=sequence,
            reason=reason,
            max_iterations=self.default_max_iterations,
        )

    def _build_realtime_route(
        self,
        *,
        is_relation: bool,
        available_tools: list[str],
    ) -> tuple[str, list[ToolCallPlan], bool, str]:
        sequence: list[ToolCallPlan] = []
        need_path_explanation = False

        if "web_search" in available_tools and "kg_hybrid_search" in available_tools:
            sequence.extend([ToolCallPlan(tool="web_search"), ToolCallPlan(tool="kg_hybrid_search")])
            strategy = "freshness_aware_search"
            reason = "The query depends on recent information, so compare live web results with graph retrieval."
        elif "web_search" in available_tools:
            sequence.append(ToolCallPlan(tool="web_search"))
            strategy = "web_search_first"
            reason = "The query depends on recent or real-time information, so web search is prioritized."
        elif "kg_hybrid_search" in available_tools:
            sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
            strategy = "kg_hybrid_fallback_for_realtime"
            reason = "Real-time web search is unavailable, so the agent falls back to knowledge retrieval."
        else:
            return "simple_answer_no_tool", [], False, "No appropriate realtime tools are available."

        if is_relation and "graph_relation_trace" in available_tools:
            sequence.append(ToolCallPlan(tool="graph_relation_trace", optional=True))
            need_path_explanation = True

        return strategy, sequence, need_path_explanation, reason

    async def _maybe_refine_with_llm(
        self,
        *,
        query: str,
        session_context: dict[str, Any] | None,
        user_profile: dict[str, Any] | None,
        available_tools: list[str],
        capability_catalog: list[dict[str, Any]],
        skill_catalog: list[dict[str, Any]],
        base_decision: RouteDecision,
    ) -> RouteDecision | None:
        if self.llm_client is None or not self.llm_client.is_available():
            return None
        if (
            base_decision.strategy == "simple_answer_no_tool"
            and not self._has_external_capabilities(capability_catalog)
            and not skill_catalog
        ):
            return None
        if (
            base_decision.strategy == "specialized_external_capability"
            and not self._has_external_capabilities(capability_catalog)
            and not skill_catalog
        ):
            return None

        system_prompt, user_prompt = build_route_judge_prompt(
            query=query,
            session_context={**(session_context or {}), "user_profile": user_profile or {}},
            available_capabilities=available_tools,
            available_capability_catalog=capability_catalog,
            available_tools=available_tools,
            available_skills=skill_catalog,
            current_plan=asdict(base_decision),
            prompt_version=self.prompt_version,
        )
        try:
            payload = await self.llm_client.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.0,
                max_tokens=700,
            )
        except Exception:
            return None

        tool_sequence = []
        for item in payload.get("tool_sequence", []):
            tool_name = item.get("tool")
            if tool_name not in available_tools:
                continue
            tool_sequence.append(
                ToolCallPlan(
                    tool=tool_name,
                    args=item.get("args", {}) if isinstance(item.get("args"), dict) else {},
                    optional=bool(item.get("optional", False)),
                    input_bindings=(
                        item.get("input_bindings")
                        if isinstance(item.get("input_bindings"), dict)
                        else {}
                    ),
                )
            )

        skill_plan = self._parse_skill_plan_payload(
            payload.get("skill_plan"),
            available_skills=skill_catalog,
        )
        if (
            skill_plan is not None
            and base_decision.skill_plan is not None
            and skill_plan.skill_name == base_decision.skill_plan.skill_name
            and not skill_plan.constraints
            and base_decision.skill_plan.constraints
        ):
            skill_plan = SkillPlan(
                skill_name=skill_plan.skill_name,
                goal=skill_plan.goal,
                reason=skill_plan.reason,
                constraints=dict(base_decision.skill_plan.constraints),
            )
        if not tool_sequence and base_decision.need_tools:
            if skill_plan is None or base_decision.skill_plan is None:
                return None
        if skill_plan is None and base_decision.skill_plan is not None:
            skill_plan = base_decision.skill_plan

        return RouteDecision(
            need_tools=bool(payload.get("need_tools", bool(tool_sequence))),
            need_memory=bool(payload.get("need_memory", base_decision.need_memory)),
            need_web_search=bool(
                payload.get("need_web_search", base_decision.need_web_search)
            ),
            need_path_explanation=bool(
                payload.get("need_path_explanation", base_decision.need_path_explanation)
            ),
            strategy=str(payload.get("strategy", base_decision.strategy)),
            tool_sequence=tool_sequence or base_decision.tool_sequence,
            reason=str(payload.get("reason", base_decision.reason)),
            max_iterations=max(
                1,
                min(int(payload.get("max_iterations", base_decision.max_iterations)), 5),
            ),
            skill_plan=skill_plan,
        )

    @staticmethod
    def _normalize_tool_sequence(
        sequence: list[ToolCallPlan], available_tools: list[str]
    ) -> list[ToolCallPlan]:
        normalized: list[ToolCallPlan] = []
        seen: set[str] = set()
        for item in sequence:
            if item.tool not in available_tools or item.tool in seen:
                continue
            normalized.append(item)
            seen.add(item.tool)
        return normalized

    @classmethod
    def _resolve_capability_catalog(
        cls,
        *,
        available_capabilities: list[Any] | None,
        available_capability_catalog: list[dict[str, Any]] | None,
        available_tools: list[str] | None,
    ) -> list[dict[str, Any]]:
        allowed_names = set(
            cls._resolve_available_capabilities(
                available_capabilities=available_capabilities,
                available_tools=available_tools,
            )
        )
        raw_catalog = (
            available_capability_catalog
            if available_capability_catalog is not None
            else available_capabilities
        )

        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_catalog or []:
            descriptor = cls._normalize_capability_descriptor(item)
            if descriptor is None:
                continue
            if allowed_names and descriptor["name"] not in allowed_names:
                continue
            if descriptor["name"] in seen:
                continue
            resolved.append(descriptor)
            seen.add(descriptor["name"])

        for name in sorted(allowed_names):
            if name in seen:
                continue
            resolved.append(
                {
                    "name": name,
                    "description": "",
                    "tags": [],
                    "kind": "native",
                    "executor": "tool_registry",
                    "arg_names": [],
                    "required_args": [],
                }
            )
        return resolved

    @staticmethod
    def _resolve_available_capabilities(
        *,
        available_capabilities: list[Any] | None,
        available_tools: list[str] | None,
    ) -> list[str]:
        raw = (
            available_capabilities
            if available_capabilities is not None
            else (available_tools or [])
        )
        resolved: list[str] = []
        seen: set[str] = set()
        for item in raw:
            name = ""
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            if not name or name in seen:
                continue
            resolved.append(name)
            seen.add(name)
        return resolved

    @staticmethod
    def _normalize_capability_descriptor(item: Any) -> dict[str, Any] | None:
        if isinstance(item, str):
            name = item.strip()
            if not name:
                return None
            return {
                "name": name,
                "description": "",
                "tags": [],
                "kind": "native",
                "executor": "tool_registry",
                "arg_names": [],
                "required_args": [],
            }
        if not isinstance(item, dict):
            return None

        name = str(item.get("name", "")).strip()
        if not name:
            return None

        input_schema = item.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = item.get("inputSchema")
        if not isinstance(input_schema, dict):
            input_schema = {}

        properties = (
            input_schema.get("properties")
            if isinstance(input_schema.get("properties"), dict)
            else {}
        )
        arg_names = item.get("arg_names")
        if not isinstance(arg_names, list):
            arg_names = sorted(
                str(key) for key in properties.keys() if isinstance(key, str) and key.strip()
            )
        required_args = item.get("required_args")
        if not isinstance(required_args, list):
            required_args = (
                input_schema.get("required")
                if isinstance(input_schema.get("required"), list)
                else []
            )
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        tags = item.get("tags")
        return {
            "name": name,
            "description": str(item.get("description", "")).strip(),
            "tags": [str(tag) for tag in tags if isinstance(tag, str)]
            if isinstance(tags, list)
            else [],
            "kind": str(item.get("kind", "native")).strip() or "native",
            "executor": str(item.get("executor", "tool_registry")).strip()
            or "tool_registry",
            "arg_names": [str(arg) for arg in arg_names if isinstance(arg, str) and arg.strip()],
            "required_args": [
                str(arg) for arg in required_args if isinstance(arg, str) and arg.strip()
            ],
            "server": str(item.get("server") or metadata.get("server") or "").strip(),
            "planner_exposed": bool(
                item.get("planner_exposed", metadata.get("planner_exposed", True))
            ),
        }

    @staticmethod
    def _resolve_skill_catalog(
        available_skills: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in available_skills or []:
            if isinstance(item, str):
                name = item.strip()
                if not name or name in seen:
                    continue
                resolved.append({"name": name, "description": "", "tags": [], "path": ""})
                seen.add(name)
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name or name in seen:
                continue
            tags = item.get("tags")
            resolved.append(
                {
                    "name": name,
                    "description": str(item.get("description", "")).strip(),
                    "tags": [str(tag) for tag in tags if isinstance(tag, str)]
                    if isinstance(tags, list)
                    else [],
                    "path": str(item.get("path", "")).strip(),
                }
            )
            seen.add(name)
        return resolved

    @staticmethod
    def _has_external_capabilities(capability_catalog: list[dict[str, Any]]) -> bool:
        return any(
            str(item.get("executor", "")).strip().lower() == "mcp"
            and str(item.get("name", "")).strip().lower() not in SKILL_INFRASTRUCTURE_TOOLS
            for item in capability_catalog
        )

    @classmethod
    def _select_matching_skill(
        cls,
        *,
        query: str,
        skill_catalog: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not skill_catalog:
            return None
        ranked = sorted(
            (
                (cls._score_search_match(query=query, item=skill), skill)
                for skill in skill_catalog
            ),
            key=lambda item: (item[0], item[1]["name"]),
            reverse=True,
        )
        best_score, best_skill = ranked[0]
        if best_score >= 6:
            return best_skill
        if SKILL_REQUEST_PATTERN.search(query or "") and best_score >= 3:
            return best_skill
        if SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN.search(query or "") and best_score >= 2:
            return best_skill
        return None

    @classmethod
    def _select_matching_external_capability(
        cls,
        *,
        query: str,
        capability_catalog: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        candidates = [
            capability
            for capability in capability_catalog
            if cls._is_external_capability(capability)
        ]
        if not candidates:
            return None
        specialized_request = bool(SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN.search(query))
        ranked = sorted(
            (
                (cls._score_search_match(query=query, item=capability), capability)
                for capability in candidates
            ),
            key=lambda item: (item[0], item[1]["name"]),
            reverse=True,
        )
        best_score, best_capability = ranked[0]
        threshold = 2 if specialized_request else 4
        if best_score >= threshold:
            return best_capability
        if specialized_request and len(candidates) == 1:
            return candidates[0]
        return None

    @staticmethod
    def _is_external_capability(capability: dict[str, Any]) -> bool:
        name = str(capability.get("name", "")).strip().lower()
        if not name or name in SKILL_INFRASTRUCTURE_TOOLS:
            return False
        kind = str(capability.get("kind", "")).strip().lower()
        executor = str(capability.get("executor", "")).strip().lower()
        return kind == "external_mcp" or executor == "mcp"

    @classmethod
    def _score_search_match(
        cls,
        *,
        query: str,
        item: dict[str, Any],
    ) -> int:
        query_lower = (query or "").strip().lower()
        if not query_lower:
            return 0
        search_text = cls._build_search_text(item)
        if not search_text:
            return 0

        score = 0
        item_name = str(item.get("name", "")).strip().lower()
        if item_name and item_name in query_lower:
            score += 5

        tags = [
            str(tag).strip().lower()
            for tag in item.get("tags", [])
            if isinstance(tag, str)
        ]
        for tag in tags:
            if len(tag) < 3:
                continue
            if tag in query_lower:
                score += 3

        for token in cls._extract_query_terms(query_lower):
            if token in search_text:
                score += 2
                if item_name and token in item_name:
                    score += 1
        return score

    @staticmethod
    def _build_search_text(item: dict[str, Any]) -> str:
        parts = [
            str(item.get("name", "")),
            str(item.get("description", "")),
            " ".join(str(tag) for tag in item.get("tags", []) if isinstance(tag, str)),
            " ".join(str(arg) for arg in item.get("arg_names", []) if isinstance(arg, str)),
            " ".join(
                str(arg) for arg in item.get("required_args", []) if isinstance(arg, str)
            ),
        ]
        return " ".join(parts).replace("_", " ").replace("-", " ").strip().lower()

    @staticmethod
    def _extract_query_terms(query: str) -> list[str]:
        terms = []
        seen: set[str] = set()
        for token in re.findall(r"[a-z][a-z0-9_./-]{2,}", query or ""):
            normalized = token.strip("_-/").lower()
            if (
                len(normalized) < 3
                or normalized in MATCH_STOPWORDS
                or normalized in seen
            ):
                continue
            seen.add(normalized)
            terms.append(normalized)
        return terms

    @staticmethod
    def _should_route_to_skill(
        *,
        query: str,
        matched_skill: dict[str, Any],
        requests_skill_workflow: bool,
        needs_specialized_external_capability: bool,
    ) -> bool:
        query_lower = (query or "").strip().lower()
        skill_name = str(matched_skill.get("name", "")).strip().lower()
        if skill_name and skill_name in query_lower:
            return True
        if requests_skill_workflow or needs_specialized_external_capability:
            return True
        return RouteJudge._score_search_match(query=query, item=matched_skill) >= 6

    @staticmethod
    def _should_route_to_external_capability(
        *,
        query: str,
        matched_capability: dict[str, Any],
    ) -> bool:
        return RouteJudge._score_search_match(query=query, item=matched_capability) >= 4

    @staticmethod
    def _build_skill_goal(query: str, matched_skill: dict[str, Any]) -> str:
        skill_name = str(matched_skill.get("name", "")).strip()
        if skill_name:
            return f"Use skill '{skill_name}' to fulfill the user request: {query}".strip()
        return query.strip()

    @classmethod
    def _build_skill_constraints(
        cls,
        *,
        query: str,
        matched_skill: dict[str, Any],
    ) -> dict[str, Any]:
        constraints: dict[str, Any] = {}
        cli_args = cls._extract_cli_args(query)
        file_paths = cls._extract_file_paths(query)
        if len(file_paths) == 1:
            constraints["input_path"] = file_paths[0]
        elif file_paths:
            constraints["input_paths"] = file_paths

        cli_input_path = cls._extract_cli_arg_value(
            cli_args,
            {"--input", "--input-path", "--file", "--path"},
        )
        if cli_input_path and "input_path" not in constraints and "input_paths" not in constraints:
            constraints["input_path"] = cli_input_path

        output_path = cls._extract_output_path(query, cli_args)
        if output_path:
            constraints["output_path"] = output_path

        format_hint = cls._extract_format_hint(query, cli_args)
        if format_hint:
            constraints["format"] = format_hint
            constraints["output_format"] = format_hint

        mode_hint = cls._extract_mode_hint(query, cli_args)
        if mode_hint:
            constraints["mode"] = mode_hint

        if cls._extract_dry_run_hint(query, cli_args):
            constraints["dry_run"] = True
            constraints["plan_only"] = True

        if cli_args:
            constraints["cli_args"] = cli_args

        if cls._has_cli_flag(cli_args, {"--overwrite"}):
            constraints["overwrite"] = True
        if cls._has_cli_flag(cli_args, {"--recursive"}):
            constraints["recursive"] = True

        shell_mode = cls._extract_shell_mode_hint(query)
        if shell_mode:
            constraints["shell_mode"] = shell_mode

        skill_name = str(matched_skill.get("name", "")).strip().lower()
        if skill_name == "xlsx":
            if FORMULA_RECALC_PATTERN.search(query or ""):
                constraints["operation"] = "recalc"
            if PRESERVE_FORMULA_PATTERN.search(query or ""):
                constraints["preserve_formulas"] = True
            if cls._has_cli_flag(
                cli_args,
                {"--preserve-formulas", "--keep-formulas", "--retain-formulas"},
            ):
                constraints["preserve_formulas"] = True
        return constraints

    @staticmethod
    def _extract_file_paths(query: str) -> list[str]:
        matches: list[str] = []
        seen: set[str] = set()
        for match in FILE_PATH_PATTERN.finditer(query or ""):
            candidate = (
                match.group("dq")
                or match.group("sq")
                or match.group("bare")
                or ""
            ).strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            matches.append(candidate)
        return matches

    @staticmethod
    def _strip_quoted_value(value: str) -> str:
        candidate = value.strip().rstrip(".,;")
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {'"', "'"}:
            return candidate[1:-1]
        return candidate

    @classmethod
    def _extract_cli_args(cls, query: str) -> list[str]:
        cli_args: list[str] = []
        for match in CLI_ARG_PATTERN.finditer(query or ""):
            flag = match.group("flag")
            if not flag:
                continue
            cli_args.append(flag.strip())
            value = match.group("value")
            if value:
                cli_args.append(cls._strip_quoted_value(value))
        return cli_args

    @staticmethod
    def _extract_cli_arg_value(
        cli_args: list[str],
        flag_names: set[str],
    ) -> str | None:
        for index, item in enumerate(cli_args):
            if item not in flag_names:
                continue
            if index + 1 >= len(cli_args):
                return None
            candidate = cli_args[index + 1]
            if candidate.startswith("-"):
                return None
            return candidate
        return None

    @staticmethod
    def _has_cli_flag(cli_args: list[str], flag_names: set[str]) -> bool:
        return any(item in flag_names for item in cli_args)

    @classmethod
    def _extract_output_path(
        cls,
        query: str,
        cli_args: list[str],
    ) -> str | None:
        cli_output = cls._extract_cli_arg_value(
            cli_args,
            {"--output", "--output-path", "-o"},
        )
        if cli_output:
            return cli_output
        match = OUTPUT_PATH_PATTERN.search(query or "")
        if match is None:
            return None
        candidate = match.group("dq") or match.group("sq") or match.group("bare") or ""
        return candidate.strip() or None

    @classmethod
    def _extract_format_hint(
        cls,
        query: str,
        cli_args: list[str],
    ) -> str | None:
        cli_value = cls._extract_cli_arg_value(
            cli_args,
            {"--format", "--output-format"},
        )
        if cli_value:
            return cli_value.lower()
        match = FORMAT_HINT_PATTERN.search(query or "")
        if match is None:
            return None
        candidate = str(match.group("format") or "").strip().lower()
        return candidate or None

    @classmethod
    def _extract_mode_hint(
        cls,
        query: str,
        cli_args: list[str],
    ) -> str | None:
        cli_value = cls._extract_cli_arg_value(cli_args, {"--mode"})
        if cli_value:
            return cli_value.lower()
        match = MODE_HINT_PATTERN.search(query or "")
        if match is None:
            return None
        candidate = str(match.group("mode") or match.group("named") or "").strip().lower()
        return candidate or None

    @classmethod
    def _extract_dry_run_hint(
        cls,
        query: str,
        cli_args: list[str],
    ) -> bool:
        return cls._has_cli_flag(cli_args, {"--dry-run", "--plan-only"}) or bool(
            DRY_RUN_PATTERN.search(query or "")
        )

    @staticmethod
    def _extract_shell_mode_hint(query: str) -> str | None:
        if FREE_SHELL_PATTERN.search(query or ""):
            return "free_shell"
        if CONSERVATIVE_SHELL_PATTERN.search(query or ""):
            return "conservative"
        return None

    @staticmethod
    def _parse_skill_plan_payload(
        payload: Any,
        *,
        available_skills: list[dict[str, Any]],
    ) -> SkillPlan | None:
        if not isinstance(payload, dict):
            return None
        skill_name = str(payload.get("skill_name", "")).strip()
        if not skill_name:
            return None
        allowed_names = {
            str(item.get("name", "")).strip()
            for item in available_skills
            if isinstance(item, dict)
        }
        if allowed_names and skill_name not in allowed_names:
            return None
        goal = str(payload.get("goal", "")).strip()
        reason = str(payload.get("reason", "")).strip() or f"Selected skill '{skill_name}'."
        constraints = payload.get("constraints")
        return SkillPlan(
            skill_name=skill_name,
            goal=goal or f"Use skill '{skill_name}' to fulfill the request.",
            reason=reason,
            constraints=constraints if isinstance(constraints, dict) else {},
        )

    @staticmethod
    def _last_turn_used_kg_tools(recent_tool_calls: Any) -> bool:
        if not isinstance(recent_tool_calls, list):
            return False
        for item in recent_tool_calls:
            if not isinstance(item, dict):
                continue
            if item.get("tool") in KG_RETRIEVAL_TOOLS:
                return True
        return False

    @staticmethod
    def _build_correction_search_query(
        query: str,
        history: list[dict[str, Any]],
    ) -> str:
        cleaned = CORRECTION_PHRASE_PATTERN.sub(" ", query or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) >= 8:
            return cleaned

        for message in reversed(history or []):
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                candidate = message["content"].strip()
                if candidate:
                    return candidate

        for message in reversed(history or []):
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                candidate = message["content"].strip()
                if candidate:
                    return candidate[:240]

        return query.strip()
