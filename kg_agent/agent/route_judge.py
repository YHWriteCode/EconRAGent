from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from kg_agent.agent.prompts import build_route_judge_prompt


FOLLOWUP_PATTERN = re.compile(
    r"(之前说过|上次提到|继续刚才|继续上个|刚才提到|earlier|previously|continue)",
    re.IGNORECASE,
)
REALTIME_PATTERN = re.compile(
    r"(最新|今日|今天|实时|近期|现在|latest|today|current|real.?time|recent)",
    re.IGNORECASE,
)
ENTITY_PATTERN = re.compile(
    r"(是什么|做什么|属于什么|是什么公司|what is|who is|what does|belongs to)",
    re.IGNORECASE,
)
RELATION_PATTERN = re.compile(
    r"(为什么|影响|传导|和谁有关|关系|关联|why|impact|affect|relation|related)",
    re.IGNORECASE,
)
QUANT_PATTERN = re.compile(
    r"(收益率|回测|夏普|最大回撤|策略表现|backtest|sharpe|drawdown|strategy)",
    re.IGNORECASE,
)
SIMPLE_PATTERN = re.compile(
    r"^\s*(hi|hello|你好|嗨|谢谢|thanks|thank you)\s*[!?。？！]*\s*$",
    re.IGNORECASE,
)
INGEST_PATTERN = re.compile(
    r"(收录|入库|写入|导入|添加到知识|存入|录入|ingest|import.*(into|to).*(kg|graph|knowledge)|add.*(to|into).*(kg|graph|knowledge)|store.*(into|to))",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s)>\"']+", re.IGNORECASE)


@dataclass
class ToolCallPlan:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    optional: bool = False


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
        has_history = bool(session_context and session_context.get("history"))

        is_simple = bool(SIMPLE_PATTERN.search(normalized_query))
        is_followup = bool(FOLLOWUP_PATTERN.search(normalized_query)) and has_history
        needs_realtime = bool(REALTIME_PATTERN.search(normalized_query))
        is_quant = bool(QUANT_PATTERN.search(normalized_query))
        is_relation = bool(RELATION_PATTERN.search(normalized_query))
        is_entity = bool(ENTITY_PATTERN.search(normalized_query))
        is_ingest = bool(INGEST_PATTERN.search(normalized_query))
        direct_urls = URL_PATTERN.findall(normalized_query)

        sequence: list[ToolCallPlan] = []
        need_memory = False
        need_web_search = False
        need_path_explanation = False
        strategy = "simple_answer_no_tool"
        reason = "The query can be handled without external tools."

        if is_simple and not any([is_quant, needs_realtime, is_relation, is_entity, is_ingest]):
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

        if is_followup and "memory_search" in available_tools:
            sequence.append(ToolCallPlan(tool="memory_search", args={"limit": 4}))
            need_memory = True

        if is_ingest and "kg_ingest" in available_tools:
            strategy = "kg_ingest_request"
            if direct_urls and "web_search" in available_tools:
                sequence.append(
                    ToolCallPlan(tool="web_search", args={"urls": direct_urls[:3]})
                )
                need_web_search = True
            sequence.append(ToolCallPlan(tool="kg_ingest"))
            reason = "The user wants to ingest content into the knowledge graph."
        elif direct_urls and "web_search" in available_tools:
            strategy = "direct_url_crawl"
            sequence.append(
                ToolCallPlan(tool="web_search", args={"urls": direct_urls[:3]})
            )
            reason = "The query includes direct URLs, so the crawler should fetch those pages first."
        elif is_quant and "quant_backtest" in available_tools:
            strategy = "quant_request"
            sequence.append(ToolCallPlan(tool="quant_backtest"))
            reason = "The query mentions trading or backtest metrics, so the quant tool should be used."
        elif needs_realtime:
            need_web_search = "web_search" in available_tools
            if "web_search" in available_tools:
                strategy = "web_search_first"
                sequence.append(ToolCallPlan(tool="web_search"))
                reason = "The query depends on recent or real-time information, so web search is prioritized."
            elif "kg_hybrid_search" in available_tools:
                strategy = "kg_hybrid_fallback_for_realtime"
                sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
                reason = "Real-time web search is unavailable, so the agent falls back to knowledge retrieval."
            if is_relation and "graph_relation_trace" in available_tools:
                sequence.append(
                    ToolCallPlan(tool="graph_relation_trace", optional=not need_web_search)
                )
                need_path_explanation = True
        elif is_relation:
            strategy = "kg_hybrid_first_then_graph_trace"
            if "kg_hybrid_search" in available_tools:
                sequence.append(ToolCallPlan(tool="kg_hybrid_search"))
            if "graph_relation_trace" in available_tools:
                sequence.append(ToolCallPlan(tool="graph_relation_trace"))
                need_path_explanation = True
            reason = "The query asks for relationships or impact chains, so retrieval and graph tracing are required."
        elif is_entity:
            if "graph_entity_lookup" in available_tools:
                strategy = "graph_entity_lookup_first"
                sequence.append(ToolCallPlan(tool="graph_entity_lookup"))
                if "kg_hybrid_search" in available_tools:
                    sequence.append(ToolCallPlan(tool="kg_hybrid_search", optional=True))
                reason = "The query asks for an entity definition, so direct graph lookup is preferred."
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
        elif need_memory and strategy == "graph_entity_lookup_first":
            strategy = "memory_first_then_entity_lookup"
        elif need_memory and strategy == "kg_hybrid_first_then_graph_trace":
            strategy = "memory_first_then_relation_trace"
            reason = "The query depends on prior context and relationship reasoning."

        sequence = self._normalize_tool_sequence(sequence, available_tools)
        return RouteDecision(
            need_tools=bool(sequence),
            need_memory=need_memory,
            need_web_search=need_web_search,
            need_path_explanation=need_path_explanation and any(
                item.tool == "graph_relation_trace" for item in sequence
            ),
            strategy=strategy,
            tool_sequence=sequence,
            reason=reason,
            max_iterations=self.default_max_iterations,
        )

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
                payload.get(
                    "need_path_explanation", base_decision.need_path_explanation
                )
            ),
            strategy=str(payload.get("strategy", base_decision.strategy)),
            tool_sequence=tool_sequence or base_decision.tool_sequence,
            reason=str(payload.get("reason", base_decision.reason)),
            max_iterations=max(
                1,
                min(
                    int(payload.get("max_iterations", base_decision.max_iterations)),
                    5,
                ),
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
