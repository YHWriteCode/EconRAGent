from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from lightrag_fork import LightRAG

from kg_agent.agent.builtin_tools import build_default_tool_registry
from kg_agent.agent.path_explainer import PathExplainer
from kg_agent.agent.prompts import build_final_answer_prompt
from kg_agent.agent.route_judge import RouteDecision, RouteJudge
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import AgentLLMClient, KGAgentConfig
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.memory.user_profile import UserProfileStore


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


@dataclass
class AgentRunContext:
    query: str
    session_id: str
    user_id: str | None = None
    workspace: str | None = None
    domain_schema: str | dict[str, Any] | None = None
    max_iterations: int | None = None
    use_memory: bool = True
    debug: bool = False
    stream: bool = False


@dataclass
class AgentResponse:
    answer: str
    route: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    path_explanation: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    streaming_supported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "route": self.route,
            "tool_calls": self.tool_calls,
            "path_explanation": self.path_explanation,
            "metadata": self.metadata,
            "streaming_supported": self.streaming_supported,
        }


@dataclass
class DynamicGraphUpdateResult:
    raw_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


RagProvider = Callable[[str | None], LightRAG | Awaitable[LightRAG]]


class AgentCore:
    def __init__(
        self,
        *,
        rag: LightRAG | None = None,
        rag_provider: RagProvider | None = None,
        config: KGAgentConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        route_judge: RouteJudge | None = None,
        path_explainer: PathExplainer | None = None,
        crawler_adapter: Crawl4AIAdapter | None = None,
        conversation_memory: ConversationMemoryStore | None = None,
        cross_session_store: CrossSessionStore | None = None,
        user_profile_store: UserProfileStore | None = None,
    ):
        self._rag = rag
        self._rag_provider = rag_provider
        self.config = config or KGAgentConfig.from_env()
        self.llm_client = AgentLLMClient(self.config.agent_model)
        self.conversation_memory = conversation_memory or ConversationMemoryStore()
        self.cross_session_store = cross_session_store or CrossSessionStore()
        self.user_profile_store = user_profile_store or UserProfileStore()
        self.tool_registry = tool_registry or build_default_tool_registry(
            self.config, self.conversation_memory
        )
        self.route_judge = route_judge or RouteJudge(
            llm_client=self.llm_client,
            default_max_iterations=self.config.runtime.max_iterations,
        )
        self.path_explainer = path_explainer or PathExplainer(
            llm_client=self.llm_client
        )
        self.crawler_adapter = crawler_adapter

    async def preview_route(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        use_memory: bool = True,
    ) -> RouteDecision:
        session_context = await self._build_session_context(
            session_id=session_id,
            use_memory=use_memory,
        )
        available_tools = self._available_tool_names(include_memory=use_memory)
        profile = await self.user_profile_store.get_profile(user_id)
        return await self.route_judge.plan(
            query=query,
            session_context=session_context,
            user_profile=profile,
            available_tools=available_tools,
        )

    async def chat(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        workspace: str | None = None,
        domain_schema: str | dict[str, Any] | None = None,
        max_iterations: int | None = None,
        use_memory: bool = True,
        debug: bool = False,
        stream: bool = False,
    ) -> AgentResponse:
        context = AgentRunContext(
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace=workspace or self.config.runtime.default_workspace or None,
            domain_schema=domain_schema or self.config.runtime.default_domain_schema,
            max_iterations=max_iterations,
            use_memory=use_memory,
            debug=debug,
            stream=stream,
        )
        rag = await self._resolve_rag(context.workspace)
        session_context = await self._build_session_context(
            session_id=context.session_id,
            use_memory=context.use_memory,
        )
        user_profile = await self.user_profile_store.get_profile(user_id)
        route = await self.route_judge.plan(
            query=context.query,
            session_context=session_context,
            user_profile=user_profile,
            available_tools=self._available_tool_names(include_memory=context.use_memory),
        )

        raw_tool_calls: list[dict[str, Any]] = []
        graph_paths: list[dict[str, Any]] = []
        evidence_chunks: list[str] = []
        execution_limit = max(
            1,
            min(context.max_iterations or route.max_iterations, route.max_iterations),
        )

        for index, tool_plan in enumerate(route.tool_sequence[:execution_limit]):
            tool_kwargs = self._build_tool_execution_kwargs(
                context=context,
                rag=rag,
                session_context=session_context,
                user_profile=user_profile,
                tool_args=tool_plan.args,
            )
            result = await self.tool_registry.execute(tool_plan.tool, **tool_kwargs)
            raw_tool_calls.append(
                self._record_tool_call(result=result, optional=tool_plan.optional)
            )
            graph_paths, evidence_chunks = self._accumulate_explanation_inputs(
                result=result,
                current_graph_paths=graph_paths,
                current_evidence=evidence_chunks,
            )
            if self._should_stop_early(
                route=route,
                tool_calls=raw_tool_calls,
                remaining=route.tool_sequence[index + 1 : execution_limit],
            ):
                break

        dynamic_update = await self._maybe_handle_dynamic_graph_update(
            context=context,
            route=route,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            raw_tool_calls=raw_tool_calls,
        )
        raw_tool_calls.extend(dynamic_update.raw_tool_calls)

        public_tool_calls = [
            self._serialize_tool_call(item, debug=context.debug) for item in raw_tool_calls
        ]
        answer_tool_calls = [
            self._serialize_tool_call(item, debug=False) for item in raw_tool_calls
        ]

        path_explanation_dict: dict[str, Any] | None = None
        if route.need_path_explanation:
            path_explanation = await self.path_explainer.explain(
                query=context.query,
                graph_paths=graph_paths,
                evidence_chunks=evidence_chunks,
                domain_schema=context.domain_schema
                if isinstance(context.domain_schema, dict)
                else {"profile_name": context.domain_schema},
            )
            path_explanation_dict = asdict(path_explanation)

        answer = await self._build_final_answer(
            query=context.query,
            route=route,
            tool_calls=answer_tool_calls,
            path_explanation=path_explanation_dict,
            conversation_history=session_context["history"],
        )

        metadata = {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "workspace": context.workspace,
            "domain_schema": context.domain_schema,
            "route_strategy": route.strategy,
            "stream_requested": context.stream,
            "streaming_supported": False,
            **dynamic_update.metadata,
        }
        answer = self._apply_answer_annotations(answer, metadata)

        if context.use_memory and self.config.tool_config.enable_memory:
            await self._persist_memory(
                context=context,
                answer=answer,
                route=route,
                raw_tool_calls=raw_tool_calls,
                response_metadata=metadata,
            )

        return AgentResponse(
            answer=answer,
            route=asdict(route),
            tool_calls=public_tool_calls,
            path_explanation=path_explanation_dict,
            metadata=metadata,
            streaming_supported=False,
        )

    async def _resolve_rag(self, workspace: str | None) -> LightRAG:
        if self._rag_provider is not None:
            resolved = self._rag_provider(workspace or "")
            if inspect.isawaitable(resolved):
                return await resolved
            return resolved
        if self._rag is None:
            raise RuntimeError("AgentCore requires a LightRAG instance or rag_provider")
        if workspace and self._rag.workspace and workspace != self._rag.workspace:
            raise RuntimeError(
                f"AgentCore is bound to workspace '{self._rag.workspace}', received '{workspace}'"
            )
        return self._rag

    async def _build_session_context(
        self,
        *,
        session_id: str,
        use_memory: bool,
    ) -> dict[str, Any]:
        if not use_memory or not self.config.tool_config.enable_memory:
            return {"history": [], "recent_tool_calls": []}
        history = await self.conversation_memory.get_recent_history(
            session_id,
            self.config.runtime.memory_window_turns,
        )
        recent_tool_calls = await self.conversation_memory.get_recent_tool_calls(
            session_id,
            assistant_turns=1,
        )
        return {"history": history, "recent_tool_calls": recent_tool_calls}

    async def _persist_memory(
        self,
        *,
        context: AgentRunContext,
        answer: str,
        route: RouteDecision,
        raw_tool_calls: list[dict[str, Any]],
        response_metadata: dict[str, Any],
    ) -> None:
        await self.conversation_memory.append_message(
            context.session_id,
            "user",
            context.query,
            metadata={"workspace": context.workspace},
        )
        await self.conversation_memory.append_message(
            context.session_id,
            "assistant",
            answer,
            metadata={
                "workspace": context.workspace,
                "route_strategy": route.strategy,
                "compact_tool_calls": self._compact_tool_calls(
                    raw_tool_calls=raw_tool_calls,
                    strategy=route.strategy,
                ),
                "response_metadata": {
                    key: value
                    for key, value in response_metadata.items()
                    if key
                    in {
                        "workspace",
                        "route_strategy",
                        "freshness_action",
                        "freshness_reason",
                        "streaming_supported",
                    }
                },
            },
        )

    async def _build_final_answer(
        self,
        *,
        query: str,
        route: RouteDecision,
        tool_calls: list[dict[str, Any]],
        path_explanation: dict[str, Any] | None,
        conversation_history: list[dict[str, str]],
    ) -> str:
        if self.llm_client.is_available():
            system_prompt, user_prompt = build_final_answer_prompt(
                query=query,
                route=asdict(route),
                tool_results=tool_calls,
                path_explanation=path_explanation,
                conversation_history=conversation_history,
            )
            try:
                text = await self.llm_client.complete_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.2,
                    max_tokens=1200,
                )
                if text.strip():
                    return text.strip()
            except Exception:
                pass
        return self._fallback_answer(query, route, tool_calls, path_explanation)

    async def _maybe_handle_dynamic_graph_update(
        self,
        *,
        context: AgentRunContext,
        route: RouteDecision,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        raw_tool_calls: list[dict[str, Any]],
    ) -> DynamicGraphUpdateResult:
        if route.strategy == "freshness_aware_search":
            return await self._handle_freshness_aware_search(
                context=context,
                rag=rag,
                session_context=session_context,
                user_profile=user_profile,
                raw_tool_calls=raw_tool_calls,
            )
        if route.strategy == "correction_and_refresh":
            return await self._handle_correction_refresh(
                context=context,
                rag=rag,
                session_context=session_context,
                user_profile=user_profile,
                raw_tool_calls=raw_tool_calls,
            )
        return DynamicGraphUpdateResult()

    async def _handle_freshness_aware_search(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        raw_tool_calls: list[dict[str, Any]],
    ) -> DynamicGraphUpdateResult:
        freshness = self._assess_graph_freshness(
            self._find_last_tool_call(raw_tool_calls, "kg_hybrid_search")
        )
        if freshness["state"] == "fresh":
            return DynamicGraphUpdateResult(
                metadata={
                    "freshness_action": "graph_data_fresh",
                    "freshness_reason": freshness["reason"],
                }
            )
        if not self.config.freshness.enable_auto_ingest:
            return DynamicGraphUpdateResult(
                metadata={
                    "freshness_action": "stale_detected_no_auto_ingest",
                    "freshness_reason": freshness["reason"],
                }
            )

        ingest_result = await self._bridge_web_pages_into_kg(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            raw_tool_calls=raw_tool_calls,
            query_override=context.query,
        )
        if not ingest_result.raw_tool_calls:
            return DynamicGraphUpdateResult(
                metadata={
                    "freshness_action": "auto_ingest_skipped",
                    "freshness_reason": freshness["reason"],
                }
            )

        last_ingest = ingest_result.raw_tool_calls[-1]
        return DynamicGraphUpdateResult(
            raw_tool_calls=ingest_result.raw_tool_calls,
            metadata={
                "freshness_action": (
                    "auto_ingested" if last_ingest["success"] else "auto_ingest_failed"
                ),
                "freshness_reason": freshness["reason"],
            },
        )

    async def _handle_correction_refresh(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        raw_tool_calls: list[dict[str, Any]],
    ) -> DynamicGraphUpdateResult:
        correction_query = self._derive_refresh_query(
            current_query=context.query,
            session_context=session_context,
        )
        ingest_result = await self._bridge_web_pages_into_kg(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            raw_tool_calls=raw_tool_calls,
            query_override=correction_query,
        )
        if not ingest_result.raw_tool_calls:
            return DynamicGraphUpdateResult(
                metadata={
                    "freshness_action": "correction_refresh_skipped",
                    "freshness_reason": (
                        "No successful web pages were available for correction refresh."
                    ),
                }
            )

        last_ingest = ingest_result.raw_tool_calls[-1]
        return DynamicGraphUpdateResult(
            raw_tool_calls=ingest_result.raw_tool_calls,
            metadata={
                "freshness_action": (
                    "user_correction_refresh"
                    if last_ingest["success"]
                    else "correction_refresh_failed"
                ),
                "freshness_reason": "Refreshed graph content using user correction feedback.",
            },
        )

    async def _bridge_web_pages_into_kg(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        raw_tool_calls: list[dict[str, Any]],
        query_override: str,
    ) -> DynamicGraphUpdateResult:
        if not self.tool_registry.has("kg_ingest"):
            return DynamicGraphUpdateResult()

        web_call = self._find_last_tool_call(raw_tool_calls, "web_search")
        pages = self._extract_successful_web_pages(web_call)
        if not pages:
            return DynamicGraphUpdateResult()

        documents = [page["markdown"] for page in pages]
        sources = [
            page.get("final_url") or page.get("url") or f"web:{index}"
            for index, page in enumerate(pages, start=1)
        ]
        base_kwargs = self._build_tool_base_kwargs(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
        )
        result = await self.tool_registry.execute(
            "kg_ingest",
            **{
                **base_kwargs,
                "query": query_override,
                "content": documents if len(documents) > 1 else documents[0],
                "source": sources if len(sources) > 1 else sources[0],
            },
        )
        return DynamicGraphUpdateResult(
            raw_tool_calls=[self._record_tool_call(result=result, optional=True)]
        )

    def _build_tool_execution_kwargs(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        tool_args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base_kwargs = self._build_tool_base_kwargs(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
        )
        if not tool_args:
            return base_kwargs

        reserved_keys = set(base_kwargs)
        sanitized_tool_args = {
            key: value for key, value in tool_args.items() if key not in reserved_keys
        }
        return {**base_kwargs, **sanitized_tool_args}

    def _build_tool_base_kwargs(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "query": context.query,
            "rag": rag,
            "session_id": context.session_id,
            "user_id": context.user_id,
            "session_context": session_context,
            "user_profile": user_profile,
            "domain_schema": context.domain_schema,
            "memory_store": self.conversation_memory,
            "crawler_adapter": self.crawler_adapter,
            "freshness_config": self.config.freshness,
        }

    @staticmethod
    def _record_tool_call(*, result, optional: bool) -> dict[str, Any]:
        return {
            "tool": result.tool_name,
            "success": result.success,
            "optional": optional,
            "summary": result.summary(),
            "error": result.error,
            "metadata": result.metadata,
            "data": result.data,
        }

    def _serialize_tool_call(
        self,
        item: dict[str, Any],
        *,
        debug: bool,
    ) -> dict[str, Any]:
        return {
            "tool": item["tool"],
            "success": item["success"],
            "optional": item.get("optional", False),
            "summary": item.get("summary"),
            "error": item.get("error"),
            "metadata": item.get("metadata") or {},
            "data": item.get("data")
            if debug
            else self._prune_tool_data(item.get("data"), tool_name=item["tool"]),
        }

    @staticmethod
    def _fallback_answer(
        query: str,
        route: RouteDecision,
        tool_calls: list[dict[str, Any]],
        path_explanation: dict[str, Any] | None,
    ) -> str:
        lines = [
            f"Question: {query}",
            f"Route strategy: {route.strategy}",
            f"Route reason: {route.reason}",
        ]
        if tool_calls:
            lines.append("Tool results:")
            for item in tool_calls:
                status = "success" if item["success"] else "failed"
                lines.append(f"- {item['tool']}: {status}, {item['summary']}")
        if path_explanation and path_explanation.get("enabled"):
            lines.append("Path explanation:")
            lines.append(path_explanation.get("final_explanation", ""))
        return "\n".join(lines)

    @staticmethod
    def _prune_tool_data(data: Any, *, tool_name: str) -> Any:
        if not isinstance(data, dict):
            return data

        summary: dict[str, Any] = {"summary": data.get("summary")}
        if "status" in data:
            summary["status"] = data["status"]

        if tool_name == "web_search":
            pages = data.get("pages", [])
            if isinstance(pages, list):
                summary["pages"] = [
                    {
                        "url": page.get("final_url") or page.get("url"),
                        "title": page.get("title"),
                        "excerpt": page.get("excerpt"),
                    }
                    for page in pages[:2]
                    if isinstance(page, dict)
                ]
                summary["page_count"] = len(pages)
            urls = data.get("urls", [])
            if isinstance(urls, list):
                summary["url_count"] = len(urls)
            return summary

        if "data" in data and isinstance(data["data"], dict):
            payload = data["data"]
            summary["counts"] = {
                key: len(value)
                for key, value in payload.items()
                if isinstance(value, list)
            }
            entities = payload.get("entities")
            if isinstance(entities, list):
                summary["entity_preview"] = [
                    item.get("entity")
                    for item in entities[:5]
                    if isinstance(item, dict) and item.get("entity")
                ]
            chunks = payload.get("chunks")
            if isinstance(chunks, list):
                summary["chunk_preview"] = [
                    {
                        "content": item.get("content"),
                        "file_path": item.get("file_path"),
                    }
                    for item in chunks[:2]
                    if isinstance(item, dict)
                ]
        if "entity_name" in data:
            summary["entity_name"] = data["entity_name"]
        if "core_entity" in data:
            summary["core_entity"] = data["core_entity"]
            summary["path_count"] = len(data.get("paths", []))
        return summary

    @staticmethod
    def _accumulate_explanation_inputs(
        *,
        result,
        current_graph_paths: list[dict[str, Any]],
        current_evidence: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        graph_paths = list(current_graph_paths)
        evidence = list(current_evidence)
        if not result.success or not isinstance(result.data, dict):
            return graph_paths, evidence
        if result.tool_name == "graph_relation_trace":
            graph_paths.extend(result.data.get("paths", []))
        if result.tool_name in {"kg_hybrid_search", "kg_naive_search"}:
            for chunk in result.data.get("data", {}).get("chunks", []):
                content = chunk.get("content")
                if isinstance(content, str) and content and content not in evidence:
                    evidence.append(content)
        return graph_paths, evidence

    @staticmethod
    def _should_stop_early(
        *,
        route: RouteDecision,
        tool_calls: list[dict[str, Any]],
        remaining: list,
    ) -> bool:
        if route.need_path_explanation or not tool_calls:
            return False
        if not tool_calls[-1]["success"]:
            return False
        return all(getattr(item, "optional", False) for item in remaining)

    def _available_tool_names(self, *, include_memory: bool) -> list[str]:
        tools = []
        for tool in self.tool_registry.list_tools():
            if not tool.enabled:
                continue
            if tool.name == "memory_search" and not include_memory:
                continue
            tools.append(tool.name)
        return tools

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            tool.to_public_dict()
            for tool in self.tool_registry.list_tools()
            if tool.enabled
        ]

    def _compact_tool_calls(
        self,
        *,
        raw_tool_calls: list[dict[str, Any]],
        strategy: str,
    ) -> list[dict[str, Any]]:
        timestamp = datetime.now(timezone.utc).isoformat()
        return [
            {
                "tool": item.get("tool"),
                "success": bool(item.get("success")),
                "summary": item.get("summary"),
                "strategy": strategy,
                "timestamp": timestamp,
            }
            for item in raw_tool_calls
        ]

    @staticmethod
    def _find_last_tool_call(
        tool_calls: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any] | None:
        for item in reversed(tool_calls):
            if item.get("tool") == tool_name:
                return item
        return None

    @staticmethod
    def _extract_successful_web_pages(
        web_call: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not web_call or not web_call.get("success"):
            return []
        data = web_call.get("data")
        if not isinstance(data, dict):
            return []
        pages = data.get("pages")
        if not isinstance(pages, list):
            return []
        return [
            page
            for page in pages
            if isinstance(page, dict)
            and page.get("success")
            and isinstance(page.get("markdown"), str)
            and page.get("markdown", "").strip()
        ]

    def _assess_graph_freshness(
        self,
        graph_call: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if graph_call is None or not graph_call.get("success"):
            return {
                "state": "gap",
                "reason": "Knowledge-graph retrieval did not return a successful result.",
            }
        payload = graph_call.get("data")
        if not isinstance(payload, dict):
            return {
                "state": "gap",
                "reason": "Knowledge-graph retrieval did not include structured data.",
            }
        data = payload.get("data")
        if not isinstance(data, dict):
            return {
                "state": "gap",
                "reason": "Knowledge-graph retrieval did not include structured data.",
            }

        entities = data.get("entities") if isinstance(data.get("entities"), list) else []
        relationships = (
            data.get("relationships") if isinstance(data.get("relationships"), list) else []
        )
        chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
        if not entities and not relationships and not chunks:
            return {
                "state": "gap",
                "reason": (
                    "Knowledge-graph retrieval returned no entities, relationships, or chunks."
                ),
            }

        timestamps = []
        for item in list(entities) + list(relationships):
            if not isinstance(item, dict):
                continue
            value = item.get("last_confirmed_at")
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                timestamps.append(datetime.fromisoformat(value))
            except ValueError:
                continue

        if timestamps:
            now = datetime.now(timezone.utc)
            average_age_seconds = sum(
                max(0.0, (now - timestamp).total_seconds()) for timestamp in timestamps
            ) / len(timestamps)
            if average_age_seconds > self.config.freshness.threshold_seconds:
                return {
                    "state": "stale",
                    "reason": (
                        f"Average graph confirmation age is {int(average_age_seconds)} seconds, "
                        f"which exceeds the threshold {self.config.freshness.threshold_seconds}."
                    ),
                }

        return {"state": "fresh", "reason": "Graph data is considered fresh enough."}

    @staticmethod
    def _derive_refresh_query(
        *,
        current_query: str,
        session_context: dict[str, Any],
    ) -> str:
        cleaned = CORRECTION_PHRASE_PATTERN.sub(" ", current_query or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) >= 8:
            return cleaned
        history = session_context.get("history", [])
        if isinstance(history, list):
            for item in reversed(history):
                if item.get("role") == "user" and isinstance(item.get("content"), str):
                    candidate = item["content"].strip()
                    if candidate:
                        return candidate
            for item in reversed(history):
                if item.get("role") == "assistant" and isinstance(item.get("content"), str):
                    candidate = item["content"].strip()
                    if candidate:
                        return candidate[:240]
        return current_query.strip()

    @staticmethod
    def _apply_answer_annotations(answer: str, metadata: dict[str, Any]) -> str:
        if metadata.get("freshness_action") == "user_correction_refresh":
            note = "Refreshed the graph using your correction feedback."
            if note not in answer:
                return f"{note}\n\n{answer}".strip()
        return answer
