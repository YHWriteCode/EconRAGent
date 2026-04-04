from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from kg_agent.agent.prompts import build_route_judge_prompt


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
QUANT_PATTERN = re.compile(
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
KG_RETRIEVAL_TOOLS = {
    "kg_hybrid_search",
    "kg_naive_search",
    "graph_entity_lookup",
    "graph_relation_trace",
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


class RouteJudge:
    def __init__(self, *, llm_client=None, default_max_iterations: int = 3):
        self.llm_client = llm_client
        self.default_max_iterations = default_max_iterations

    async def plan(
        self,
        query: str,
        session_context: dict[str, Any] | None,
        user_profile: dict[str, Any] | None,
        available_tools: list[str],
    ) -> RouteDecision:
        base_decision = self._rule_based_fallback(
            query=query,
            session_context=session_context,
            available_tools=available_tools,
        )
        refined = await self._maybe_refine_with_llm(
            query=query,
            session_context=session_context,
            user_profile=user_profile,
            available_tools=available_tools,
            base_decision=base_decision,
        )
        return refined or base_decision

    def _rule_based_fallback(
        self,
        *,
        query: str,
        session_context: dict[str, Any] | None,
        available_tools: list[str],
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

        is_simple = bool(SIMPLE_PATTERN.search(normalized_query))
        is_followup = bool(FOLLOWUP_PATTERN.search(normalized_query)) and (
            has_history or has_cross_session_tool
        )
        needs_realtime = bool(REALTIME_PATTERN.search(normalized_query))
        is_quant = bool(QUANT_PATTERN.search(normalized_query))
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
            [is_quant, needs_realtime, is_relation, is_entity, is_ingest, is_correction]
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
            sequence.append(
                ToolCallPlan(tool="web_search", args={"urls": direct_urls[:3]})
            )
            reason = (
                "The query includes direct URLs, so the crawler should fetch those pages first."
            )
        elif is_quant and "quant_backtest" in available_tools:
            strategy = "quant_request"
            sequence.append(ToolCallPlan(tool="quant_backtest"))
            reason = (
                "The query mentions trading or backtest metrics, so the quant tool should be used."
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
                reason = (
                    "Hybrid retrieval is the default strategy for factual knowledge questions."
                )

        if need_memory and strategy == "factual_qa":
            strategy = "memory_first_then_hybrid"
            reason = (
                "The query continues prior context, so memory is checked before hybrid retrieval."
            )
        elif need_memory and strategy == "simple_answer_no_tool":
            strategy = "cross_session_memory_first"
            reason = (
                "The query continues prior context and needs memory from earlier sessions."
            )
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
            sequence.extend(
                [
                    ToolCallPlan(tool="web_search"),
                    ToolCallPlan(tool="kg_hybrid_search"),
                ]
            )
            strategy = "freshness_aware_search"
            reason = (
                "The query depends on recent information, so compare live web results with graph retrieval."
            )
        elif "web_search" in available_tools:
            sequence.append(ToolCallPlan(tool="web_search"))
            strategy = "web_search_first"
            reason = (
                "The query depends on recent or real-time information, so web search is prioritized."
            )
        elif "kg_hybrid_search" in available_tools:
            sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
            strategy = "kg_hybrid_fallback_for_realtime"
            reason = (
                "Real-time web search is unavailable, so the agent falls back to knowledge retrieval."
            )
        else:
            return (
                "simple_answer_no_tool",
                [],
                False,
                "No appropriate realtime tools are available.",
            )

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
        base_decision: RouteDecision,
    ) -> RouteDecision | None:
        if self.llm_client is None or not self.llm_client.is_available():
            return None
        if base_decision.strategy in {"simple_answer_no_tool", "quant_request"}:
            return None

        system_prompt, user_prompt = build_route_judge_prompt(
            query=query,
            session_context={**(session_context or {}), "user_profile": user_profile or {}},
            available_tools=available_tools,
            current_plan=asdict(base_decision),
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

        if not tool_sequence and base_decision.need_tools:
            return None

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
