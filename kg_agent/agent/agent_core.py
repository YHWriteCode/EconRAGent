from __future__ import annotations

import inspect
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from lightrag_fork import LightRAG

from kg_agent.agent.builtin_tools import build_default_tool_registry
from kg_agent.agent.capability_registry import (
    CapabilityDefinition,
    CapabilityRegistry,
    add_mcp_capabilities,
    build_native_capability_registry,
)
from kg_agent.agent.path_explainer import PathExplainer
from kg_agent.agent.prompts import build_final_answer_prompt
from kg_agent.agent.route_judge import RouteDecision, RouteJudge
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import AgentLLMClient, FallbackLLMClient, KGAgentConfig
from kg_agent.crawler.crawl_state_store import CrawlStateStore
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
from kg_agent.mcp.adapter import MCPAdapter
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.memory.user_profile import UserProfileStore
from kg_agent.skills import SkillExecutor, SkillLoader, SkillRegistry


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
class CapabilityInvocationResponse:
    capability: dict[str, Any]
    result: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "result": self.result,
            "metadata": self.metadata,
        }


@dataclass
class DynamicGraphUpdateResult:
    raw_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedAgentRun:
    context: AgentRunContext
    session_context: dict[str, Any]
    route: RouteDecision
    raw_tool_calls: list[dict[str, Any]]
    public_tool_calls: list[dict[str, Any]]
    answer_tool_calls: list[dict[str, Any]]
    path_explanation_dict: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass
class PlannedAgentRun:
    context: AgentRunContext
    rag: LightRAG
    session_context: dict[str, Any]
    user_profile: dict[str, Any] | None
    route: RouteDecision
    execution_limit: int


RagProvider = Callable[[str | None], LightRAG | Awaitable[LightRAG]]


class AgentCore:
    def __init__(
        self,
        *,
        rag: LightRAG | None = None,
        rag_provider: RagProvider | None = None,
        config: KGAgentConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        capability_registry: CapabilityRegistry | None = None,
        mcp_adapter: MCPAdapter | None = None,
        route_judge: RouteJudge | None = None,
        path_explainer: PathExplainer | None = None,
        crawler_adapter: Crawl4AIAdapter | None = None,
        crawl_state_store: CrawlStateStore | None = None,
        conversation_memory: ConversationMemoryStore | None = None,
        cross_session_store: CrossSessionStore | None = None,
        user_profile_store: UserProfileStore | None = None,
        skill_registry: SkillRegistry | None = None,
        skill_loader: SkillLoader | None = None,
        skill_executor: SkillExecutor | None = None,
    ):
        self._rag = rag
        self._rag_provider = rag_provider
        self.config = config or KGAgentConfig.from_env()
        self.llm_client = AgentLLMClient(self.config.agent_model)
        utility_model_config = getattr(self.config, "utility_model", None)
        primary_utility_client = (
            AgentLLMClient(utility_model_config)
            if utility_model_config is not None
            else None
        )
        self.utility_llm_client = FallbackLLMClient(
            primary=primary_utility_client,
            fallback=self.llm_client,
            label="kg_agent internal utility work",
        )
        self.conversation_memory = conversation_memory or ConversationMemoryStore()
        self.cross_session_store = cross_session_store or CrossSessionStore(
            conversation_memory=self.conversation_memory
        )
        self.user_profile_store = user_profile_store or UserProfileStore()
        self.skill_registry = skill_registry or SkillRegistry(
            self.config.runtime.skills_dir
        )
        self.skill_loader = skill_loader or SkillLoader(self.skill_registry)
        self.skill_executor = skill_executor or SkillExecutor(
            registry=self.skill_registry,
            loader=self.skill_loader,
        )
        self.tool_registry = tool_registry or build_default_tool_registry(
            self.config, self.conversation_memory, self.cross_session_store
        )
        self.mcp_adapter = mcp_adapter
        self.capability_registry = capability_registry or build_native_capability_registry(
            self.tool_registry
        )
        if self.mcp_adapter is not None:
            add_mcp_capabilities(
                self.capability_registry,
                self.mcp_adapter.list_capability_configs(),
            )
        self.route_judge = route_judge or RouteJudge(
            llm_client=self.utility_llm_client,
            default_max_iterations=self.config.runtime.max_iterations,
            prompt_version=self.config.runtime.route_judge_prompt_version,
        )
        self.path_explainer = path_explainer or PathExplainer(
            llm_client=self.utility_llm_client
        )
        self.crawler_adapter = crawler_adapter
        self.crawl_state_store = crawl_state_store

    async def initialize_external_capabilities(self) -> list[dict[str, Any]]:
        if self.mcp_adapter is None:
            return []
        discovered = await self.mcp_adapter.discover_capabilities(
            reserved_names={
                capability.name
                for capability in self.capability_registry.list_capabilities()
            },
        )
        if discovered:
            add_mcp_capabilities(
                self.capability_registry,
                discovered,
                skip_existing=True,
            )
        return [CapabilityDefinition.from_mcp_capability_config(item).to_public_dict() for item in discovered]

    async def preview_route(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        use_memory: bool = True,
    ) -> RouteDecision:
        await self.initialize_external_capabilities()
        session_context = await self._build_session_context(
            query=query,
            session_id=session_id,
            use_memory=use_memory,
            user_id=user_id,
        )
        available_capabilities = self._available_capability_names(
            include_memory=use_memory,
            include_cross_session=bool(user_id),
        )
        available_capability_catalog = self._available_capability_catalog(
            include_memory=use_memory,
            include_cross_session=bool(user_id),
        )
        profile = await self.user_profile_store.get_profile(user_id)
        return await self.route_judge.plan(
            query=query,
            session_context=session_context,
            user_profile=profile,
            available_capabilities=available_capabilities,
            available_capability_catalog=available_capability_catalog,
            available_skills=self._available_skill_catalog(),
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
        await self.initialize_external_capabilities()
        prepared = await self._prepare_agent_run(
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
        answer = await self._build_final_answer(
            query=prepared.context.query,
            route=prepared.route,
            tool_calls=prepared.answer_tool_calls,
            path_explanation=prepared.path_explanation_dict,
            conversation_history=prepared.session_context["history"],
        )
        answer = self._apply_answer_annotations(answer, prepared.metadata)

        if prepared.context.use_memory and self.config.tool_config.enable_memory:
            await self._persist_memory(
                context=prepared.context,
                answer=answer,
                route=prepared.route,
                raw_tool_calls=prepared.raw_tool_calls,
                response_metadata=prepared.metadata,
            )

        return AgentResponse(
            answer=answer,
            route=asdict(prepared.route),
            tool_calls=prepared.public_tool_calls,
            path_explanation=prepared.path_explanation_dict,
            metadata=prepared.metadata,
            streaming_supported=True,
        )

    async def invoke_capability(
        self,
        *,
        capability_name: str,
        session_id: str,
        query: str = "",
        user_id: str | None = None,
        workspace: str | None = None,
        domain_schema: str | dict[str, Any] | None = None,
        use_memory: bool = False,
        args: dict[str, Any] | None = None,
    ) -> CapabilityInvocationResponse:
        await self.initialize_external_capabilities()
        capability = self._require_enabled_capability(capability_name)
        context = AgentRunContext(
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace=workspace or self.config.runtime.default_workspace or None,
            domain_schema=domain_schema or self.config.runtime.default_domain_schema,
            use_memory=use_memory,
        )
        rag = None
        if capability.executor == "tool_registry":
            rag = await self._resolve_rag(context.workspace)
        session_context = await self._build_session_context(
            query=context.query,
            session_id=context.session_id,
            use_memory=context.use_memory,
            user_id=context.user_id,
        )
        user_profile = await self.user_profile_store.get_profile(user_id)
        result = await self._execute_capability(
            capability_name=capability_name,
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            capability_args=args,
        )
        recorded = self._record_tool_call(result=result, optional=False)
        return CapabilityInvocationResponse(
            capability=capability.to_public_dict(),
            result=self._serialize_tool_call(recorded, debug=True),
            metadata=self._build_capability_invocation_metadata(
                context=context,
                capability=capability,
            ),
        )

    async def chat_stream(
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
    ) -> AsyncIterator[dict[str, Any]]:
        await self.initialize_external_capabilities()
        planned = await self._plan_agent_run(
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace=workspace or self.config.runtime.default_workspace or None,
            domain_schema=domain_schema or self.config.runtime.default_domain_schema,
            max_iterations=max_iterations,
            use_memory=use_memory,
            debug=debug,
            stream=True,
        )

        metadata = self._build_response_metadata(planned.context, planned.route)
        yield {
            "type": "meta",
            "metadata": metadata,
        }
        yield {"type": "route", "route": asdict(planned.route)}

        raw_tool_calls: list[dict[str, Any]] = []
        graph_paths: list[dict[str, Any]] = []
        evidence_chunks: list[str] = []

        for index, tool_plan in enumerate(
            planned.route.tool_sequence[: planned.execution_limit],
            start=1,
        ):
            yield {
                "type": "tool_start",
                "index": index,
                "total": planned.execution_limit,
                "tool": tool_plan.tool,
                "optional": tool_plan.optional,
            }
            result = await self._execute_tool_plan(
                context=planned.context,
                rag=planned.rag,
                session_context=planned.session_context,
                user_profile=planned.user_profile,
                tool_plan=tool_plan,
                raw_tool_calls=raw_tool_calls,
            )
            recorded_call = self._record_tool_call(
                result=result,
                optional=tool_plan.optional,
            )
            raw_tool_calls.append(recorded_call)
            graph_paths, evidence_chunks = self._accumulate_explanation_inputs(
                result=result,
                current_graph_paths=graph_paths,
                current_evidence=evidence_chunks,
            )
            yield {
                "type": "tool_result",
                "index": index,
                "tool_call": self._serialize_tool_call(
                    recorded_call,
                    debug=planned.context.debug,
                ),
            }
            if self._should_stop_early(
                route=planned.route,
                tool_calls=raw_tool_calls,
                remaining=planned.route.tool_sequence[index : planned.execution_limit],
            ):
                yield {
                    "type": "tool_skip",
                    "reason": "optional_tools_skipped_after_success",
                }
                break

        if planned.route.skill_plan is not None:
            yield {
                "type": "skill_start",
                "skill_name": planned.route.skill_plan.skill_name,
            }
            skill_result = await self._execute_skill_plan(
                context=planned.context,
                skill_plan=planned.route.skill_plan,
            )
            recorded_call = self._record_tool_call(result=skill_result, optional=False)
            raw_tool_calls.append(recorded_call)
            yield {
                "type": "skill_result",
                "skill_call": self._serialize_tool_call(
                    recorded_call,
                    debug=planned.context.debug,
                ),
            }

        dynamic_update = await self._maybe_handle_dynamic_graph_update(
            context=planned.context,
            route=planned.route,
            rag=planned.rag,
            session_context=planned.session_context,
            user_profile=planned.user_profile,
            raw_tool_calls=raw_tool_calls,
        )
        if dynamic_update.metadata:
            metadata.update(dynamic_update.metadata)
            yield {
                "type": "status",
                "metadata": dynamic_update.metadata,
            }
        if dynamic_update.raw_tool_calls:
            for item in dynamic_update.raw_tool_calls:
                raw_tool_calls.append(item)
                yield {
                    "type": "tool_result",
                    "phase": "dynamic_update",
                    "tool_call": self._serialize_tool_call(
                        item,
                        debug=planned.context.debug,
                    ),
                }

        if planned.route.need_path_explanation:
            yield {"type": "path_explanation_start"}
        path_explanation_dict = await self._build_path_explanation_dict(
            context=planned.context,
            route=planned.route,
            graph_paths=graph_paths,
            evidence_chunks=evidence_chunks,
        )
        if path_explanation_dict is not None:
            yield {
                "type": "path_explanation",
                "path_explanation": path_explanation_dict,
            }

        prepared = self._assemble_prepared_agent_run(
            planned=planned,
            raw_tool_calls=raw_tool_calls,
            path_explanation_dict=path_explanation_dict,
            metadata=metadata,
        )

        chunks: list[str] = []
        yield {"type": "answer_start"}
        annotation_prefix = self._build_answer_annotation_prefix(metadata)
        if annotation_prefix:
            chunks.append(annotation_prefix)
            yield {"type": "delta", "content": annotation_prefix}

        streamed = False
        if self.llm_client.is_available():
            try:
                async for delta in self._build_final_answer_stream(
                    query=prepared.context.query,
                    route=prepared.route,
                    tool_calls=prepared.answer_tool_calls,
                    path_explanation=prepared.path_explanation_dict,
                    conversation_history=prepared.session_context["history"],
                ):
                    if not delta:
                        continue
                    streamed = True
                    chunks.append(delta)
                    yield {"type": "delta", "content": delta}
            except Exception:
                streamed = False

        if not streamed:
            fallback_answer = self._fallback_answer(
                prepared.context.query,
                prepared.route,
                prepared.answer_tool_calls,
                prepared.path_explanation_dict,
            )
            if not chunks or fallback_answer not in "".join(chunks):
                chunks.append(fallback_answer)
                yield {"type": "delta", "content": fallback_answer}

        answer = "".join(chunks).strip()
        if prepared.context.use_memory and self.config.tool_config.enable_memory:
            await self._persist_memory(
                context=prepared.context,
                answer=answer,
                route=prepared.route,
                raw_tool_calls=prepared.raw_tool_calls,
                response_metadata=metadata,
            )

        yield {
            "type": "done",
            "answer": answer,
            "route": asdict(prepared.route),
            "tool_calls": prepared.public_tool_calls,
            "path_explanation": prepared.path_explanation_dict,
            "metadata": metadata,
        }

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

    async def _prepare_agent_run(
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
    ) -> PreparedAgentRun:
        planned = await self._plan_agent_run(
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace=workspace,
            domain_schema=domain_schema,
            max_iterations=max_iterations,
            use_memory=use_memory,
            debug=debug,
            stream=stream,
        )
        raw_tool_calls: list[dict[str, Any]] = []
        graph_paths: list[dict[str, Any]] = []
        evidence_chunks: list[str] = []

        for index, tool_plan in enumerate(
            planned.route.tool_sequence[: planned.execution_limit]
        ):
            result = await self._execute_tool_plan(
                context=planned.context,
                rag=planned.rag,
                session_context=planned.session_context,
                user_profile=planned.user_profile,
                tool_plan=tool_plan,
                raw_tool_calls=raw_tool_calls,
            )
            raw_tool_calls.append(
                self._record_tool_call(result=result, optional=tool_plan.optional)
            )
            graph_paths, evidence_chunks = self._accumulate_explanation_inputs(
                result=result,
                current_graph_paths=graph_paths,
                current_evidence=evidence_chunks,
            )
            if self._should_stop_early(
                route=planned.route,
                tool_calls=raw_tool_calls,
                remaining=planned.route.tool_sequence[index + 1 : planned.execution_limit],
            ):
                break

        if planned.route.skill_plan is not None:
            skill_result = await self._execute_skill_plan(
                context=planned.context,
                skill_plan=planned.route.skill_plan,
            )
            raw_tool_calls.append(
                self._record_tool_call(result=skill_result, optional=False)
            )

        dynamic_update = await self._maybe_handle_dynamic_graph_update(
            context=planned.context,
            route=planned.route,
            rag=planned.rag,
            session_context=planned.session_context,
            user_profile=planned.user_profile,
            raw_tool_calls=raw_tool_calls,
        )
        raw_tool_calls.extend(dynamic_update.raw_tool_calls)

        path_explanation_dict = await self._build_path_explanation_dict(
            context=planned.context,
            route=planned.route,
            graph_paths=graph_paths,
            evidence_chunks=evidence_chunks,
        )
        metadata = self._build_response_metadata(
            planned.context,
            planned.route,
            extra_metadata=dynamic_update.metadata,
        )
        return self._assemble_prepared_agent_run(
            planned=planned,
            raw_tool_calls=raw_tool_calls,
            path_explanation_dict=path_explanation_dict,
            metadata=metadata,
        )

    async def _plan_agent_run(
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
    ) -> PlannedAgentRun:
        context = AgentRunContext(
            query=query,
            session_id=session_id,
            user_id=user_id,
            workspace=workspace,
            domain_schema=domain_schema,
            max_iterations=max_iterations,
            use_memory=use_memory,
            debug=debug,
            stream=stream,
        )
        rag = await self._resolve_rag(context.workspace)
        session_context = await self._build_session_context(
            query=context.query,
            session_id=context.session_id,
            use_memory=context.use_memory,
            user_id=context.user_id,
        )
        user_profile = await self.user_profile_store.get_profile(user_id)
        route = await self.route_judge.plan(
            query=context.query,
            session_context=session_context,
            user_profile=user_profile,
            available_capabilities=self._available_capability_names(
                include_memory=context.use_memory,
                include_cross_session=bool(context.user_id),
            ),
            available_capability_catalog=self._available_capability_catalog(
                include_memory=context.use_memory,
                include_cross_session=bool(context.user_id),
            ),
            available_skills=self._available_skill_catalog(),
        )

        execution_limit = max(
            1,
            min(context.max_iterations or route.max_iterations, route.max_iterations),
        )
        return PlannedAgentRun(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            route=route,
            execution_limit=execution_limit,
        )

    async def _build_session_context(
        self,
        *,
        query: str,
        session_id: str,
        use_memory: bool,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if not use_memory or not self.config.tool_config.enable_memory:
            return {"history": [], "recent_tool_calls": []}
        history = await self.conversation_memory.get_context_window(
            session_id,
            query=query,
            turns=self.config.runtime.memory_window_turns,
            min_recent_turns=self.config.runtime.memory_min_recent_turns,
            max_tokens=self.config.runtime.memory_max_context_tokens,
        )
        recent_tool_calls = await self.conversation_memory.get_recent_tool_calls(
            session_id,
            assistant_turns=1,
        )
        return {
            "history": history,
            "recent_tool_calls": recent_tool_calls,
            "cross_session_enabled": bool(user_id),
        }

    async def _build_path_explanation_dict(
        self,
        *,
        context: AgentRunContext,
        route: RouteDecision,
        graph_paths: list[dict[str, Any]],
        evidence_chunks: list[str],
    ) -> dict[str, Any] | None:
        if not route.need_path_explanation:
            return None
        path_explanation = await self.path_explainer.explain(
            query=context.query,
            graph_paths=graph_paths,
            evidence_chunks=evidence_chunks,
            domain_schema=context.domain_schema
            if isinstance(context.domain_schema, dict)
            else {"profile_name": context.domain_schema},
        )
        return asdict(path_explanation)

    @staticmethod
    def _build_response_metadata(
        context: AgentRunContext,
        route: RouteDecision,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "workspace": context.workspace,
            "domain_schema": context.domain_schema,
            "route_strategy": route.strategy,
            "skill_name": (
                route.skill_plan.skill_name if getattr(route, "skill_plan", None) else None
            ),
            "stream_requested": context.stream,
            "streaming_supported": True,
            **(extra_metadata or {}),
        }

    def _assemble_prepared_agent_run(
        self,
        *,
        planned: PlannedAgentRun,
        raw_tool_calls: list[dict[str, Any]],
        path_explanation_dict: dict[str, Any] | None,
        metadata: dict[str, Any],
    ) -> PreparedAgentRun:
        public_tool_calls = [
            self._serialize_tool_call(item, debug=planned.context.debug)
            for item in raw_tool_calls
        ]
        answer_tool_calls = [
            self._serialize_tool_call(item, debug=False) for item in raw_tool_calls
        ]
        return PreparedAgentRun(
            context=planned.context,
            session_context=planned.session_context,
            route=planned.route,
            raw_tool_calls=raw_tool_calls,
            public_tool_calls=public_tool_calls,
            answer_tool_calls=answer_tool_calls,
            path_explanation_dict=path_explanation_dict,
            metadata=metadata,
        )

    async def _persist_memory(
        self,
        *,
        context: AgentRunContext,
        answer: str,
        route: RouteDecision,
        raw_tool_calls: list[dict[str, Any]],
        response_metadata: dict[str, Any],
    ) -> None:
        user_message = await self.conversation_memory.append_message(
            context.session_id,
            "user",
            context.query,
            metadata={"workspace": context.workspace},
            user_id=context.user_id,
        )
        assistant_message = await self.conversation_memory.append_message(
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
            user_id=context.user_id,
        )
        if self.cross_session_store is not None and context.user_id:
            await self.cross_session_store.index_message(user_message)
            await self.cross_session_store.index_message(assistant_message)

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

    async def _build_final_answer_stream(
        self,
        *,
        query: str,
        route: RouteDecision,
        tool_calls: list[dict[str, Any]],
        path_explanation: dict[str, Any] | None,
        conversation_history: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        system_prompt, user_prompt = build_final_answer_prompt(
            query=query,
            route=asdict(route),
            tool_results=tool_calls,
            path_explanation=path_explanation,
            conversation_history=conversation_history,
        )
        async for delta in self.llm_client.stream_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            max_tokens=1200,
        ):
            yield delta

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
        existing_ingest = self._find_last_tool_call(raw_tool_calls, "kg_ingest")
        ingest_result = DynamicGraphUpdateResult()
        if existing_ingest is None:
            ingest_result = await self._bridge_web_pages_into_kg(
                context=context,
                rag=rag,
                session_context=session_context,
                user_profile=user_profile,
                raw_tool_calls=raw_tool_calls,
                query_override=correction_query,
            )

        last_ingest = existing_ingest or (
            ingest_result.raw_tool_calls[-1] if ingest_result.raw_tool_calls else None
        )
        if last_ingest is None:
            return DynamicGraphUpdateResult(
                metadata={
                    "freshness_action": "correction_refresh_skipped",
                    "freshness_reason": (
                        "No successful web pages were available for correction refresh."
                    ),
                }
            )

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

        plan = type("ToolPlanLike", (), {})()
        plan.tool = "kg_ingest"
        plan.args = {}
        plan.optional = True
        plan.input_bindings = {
            "content": {
                "from": "web_search",
                "transform": "web_pages_markdown",
            },
            "source": {
                "from": "web_search",
                "transform": "web_pages_sources",
            },
        }
        result = await self._execute_tool_plan(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            tool_plan=plan,
            raw_tool_calls=raw_tool_calls,
            query_override=query_override,
        )
        return DynamicGraphUpdateResult(
            raw_tool_calls=[self._record_tool_call(result=result, optional=True)]
        )

    async def _execute_tool_plan(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        tool_plan: Any,
        raw_tool_calls: list[dict[str, Any]],
        query_override: str | None = None,
    ):
        return await self._execute_capability(
            capability_name=tool_plan.tool,
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            capability_args=getattr(tool_plan, "args", None),
            raw_tool_calls=raw_tool_calls,
            query_override=query_override,
            input_bindings=getattr(tool_plan, "input_bindings", {}) or {},
        )

    async def _execute_skill_plan(
        self,
        *,
        context: AgentRunContext,
        skill_plan,
    ):
        return await self.skill_executor.execute(
            skill_name=skill_plan.skill_name,
            goal=skill_plan.goal,
            user_query=context.query,
            workspace=context.workspace,
            constraints=getattr(skill_plan, "constraints", {}) or {},
        )

    async def _execute_capability(
        self,
        *,
        capability_name: str,
        context: AgentRunContext,
        rag: LightRAG | None,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
        capability_args: dict[str, Any] | None,
        raw_tool_calls: list[dict[str, Any]] | None = None,
        query_override: str | None = None,
        input_bindings: dict[str, dict[str, Any]] | None = None,
    ):
        tool_kwargs = self._build_tool_execution_kwargs(
            context=context,
            rag=rag,
            session_context=session_context,
            user_profile=user_profile,
            tool_args=capability_args,
        )
        if query_override is not None:
            tool_kwargs["query"] = query_override
        if isinstance(input_bindings, dict):
            tool_kwargs.update(
                self._resolve_tool_input_bindings(
                    input_bindings=input_bindings,
                    raw_tool_calls=raw_tool_calls or [],
                )
            )
        capability = self.capability_registry.get(capability_name)
        if capability is not None and capability.executor == "mcp":
            if self.mcp_adapter is None:
                raise RuntimeError(
                    f"MCP adapter is not configured for capability '{capability_name}'"
                )
            return await self.mcp_adapter.invoke(capability_name, tool_kwargs)

        target_name = (
            capability.target_name
            if capability is not None and isinstance(capability.target_name, str)
            else capability_name
        )
        result = await self.tool_registry.execute(target_name, **tool_kwargs)
        if result.tool_name != capability_name:
            result.tool_name = capability_name
        return result

    def _build_tool_execution_kwargs(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG | None,
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

    def _resolve_tool_input_bindings(
        self,
        *,
        input_bindings: dict[str, dict[str, Any]],
        raw_tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for arg_name, binding in input_bindings.items():
            if not isinstance(binding, dict):
                continue
            value = self._resolve_tool_binding(
                binding=binding,
                raw_tool_calls=raw_tool_calls,
            )
            if value is not None:
                resolved[arg_name] = value
        return resolved

    def _resolve_tool_binding(
        self,
        *,
        binding: dict[str, Any],
        raw_tool_calls: list[dict[str, Any]],
    ) -> Any:
        source_name = str(binding.get("from") or "previous").strip().lower()
        if source_name in {"previous", "__previous__"}:
            source_call = raw_tool_calls[-1] if raw_tool_calls else None
        else:
            source_call = self._find_last_tool_call(raw_tool_calls, source_name)
        if not source_call:
            return None
        if not bool(binding.get("allow_failed", False)) and not source_call.get("success"):
            return None

        transform = binding.get("transform")
        if isinstance(transform, str) and transform.strip():
            return self._apply_tool_binding_transform(source_call, transform.strip())

        path = binding.get("path")
        if isinstance(path, str) and path.strip():
            return self._extract_binding_path(source_call, path.strip())

        return source_call.get("data")

    def _apply_tool_binding_transform(
        self,
        source_call: dict[str, Any],
        transform: str,
    ) -> Any:
        structured_payload = self._extract_tool_structured_payload(source_call)
        if transform == "web_pages_markdown":
            pages = self._extract_successful_web_pages(source_call)
            documents = [
                page["markdown"]
                for page in pages
                if isinstance(page.get("markdown"), str) and page["markdown"].strip()
            ]
            if not documents:
                return None
            return documents[0] if len(documents) == 1 else documents
        if transform == "web_pages_sources":
            pages = self._extract_successful_web_pages(source_call)
            sources = [
                page.get("final_url") or page.get("url") or f"web:{index}"
                for index, page in enumerate(pages, start=1)
            ]
            if not sources:
                return None
            return sources[0] if len(sources) == 1 else sources
        if transform == "selected_skill_name":
            selected_name = self._extract_binding_path(
                structured_payload,
                "selected_skill_name",
            )
            if isinstance(selected_name, str) and selected_name.strip():
                return selected_name.strip()
            selected_name = self._extract_binding_path(
                structured_payload,
                "selected_skill.name",
            )
            if isinstance(selected_name, str) and selected_name.strip():
                return selected_name.strip()
            skills = self._extract_binding_path(structured_payload, "skills[]")
            if isinstance(skills, list):
                for item in skills:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip()
                        if name:
                            return name
        if transform == "skill_name":
            for path in ("name", "skill_name", "selected_skill_name"):
                value = self._extract_binding_path(structured_payload, path)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if transform == "default_skill_script":
            for path in (
                "default_script",
                "recommended_script",
                "metadata.default_script",
                "metadata.recommended_script",
                "metadata.entrypoint",
                "metadata.script",
            ):
                value = self._extract_binding_path(structured_payload, path)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            scripts = self._extract_binding_path(structured_payload, "scripts[]")
            if isinstance(scripts, list):
                for item in scripts:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
        return None

    @staticmethod
    def _extract_tool_structured_payload(source_call: dict[str, Any]) -> Any:
        data = source_call.get("data")
        if isinstance(data, dict):
            structured = data.get("structured_content")
            if isinstance(structured, dict):
                return structured
        return data

    @classmethod
    def _extract_binding_path(cls, value: Any, path: str) -> Any:
        current: list[Any] = [value]
        for raw_token in path.split("."):
            token = raw_token.strip()
            if not token:
                continue
            expand = token.endswith("[]")
            key = token[:-2] if expand else token
            next_values: list[Any] = []
            for item in current:
                extracted = cls._extract_binding_token(item, key)
                if extracted is None:
                    continue
                if expand:
                    if isinstance(extracted, list):
                        next_values.extend(extracted)
                    else:
                        next_values.append(extracted)
                else:
                    next_values.append(extracted)
            current = next_values
            if not current:
                return None
        if not current:
            return None
        return current[0] if len(current) == 1 else current

    @staticmethod
    def _extract_binding_token(value: Any, token: str) -> Any:
        if not token:
            return value
        if isinstance(value, dict):
            return value.get(token)
        return getattr(value, token, None)

    def _build_tool_base_kwargs(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG | None,
        session_context: dict[str, Any],
        user_profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base_kwargs = {
            "query": context.query,
            "session_id": context.session_id,
            "user_id": context.user_id,
            "session_context": session_context,
            "user_profile": user_profile,
            "domain_schema": context.domain_schema,
            "memory_store": self.conversation_memory,
            "cross_session_store": self.cross_session_store,
            "crawler_adapter": self.crawler_adapter,
            "crawl_state_store": self.crawl_state_store,
            "freshness_config": self.config.freshness,
        }
        if rag is not None:
            base_kwargs["rag"] = rag
        return base_kwargs

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

        if tool_name.startswith("skill:"):
            for key in ("skill_name", "goal", "workspace"):
                if key in data:
                    summary[key] = data[key]
            skill = data.get("skill")
            if isinstance(skill, dict):
                summary["skill"] = {
                    "name": skill.get("name"),
                    "description": skill.get("description"),
                    "path": skill.get("path"),
                }
            files = data.get("file_inventory")
            if isinstance(files, list):
                summary["file_count"] = len(files)
            return summary

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
        if not remaining:
            return False
        if not tool_calls[-1]["success"]:
            return False
        return all(getattr(item, "optional", False) for item in remaining)

    def _available_capability_names(
        self,
        *,
        include_memory: bool,
        include_cross_session: bool = False,
    ) -> list[str]:
        return [
            item["name"]
            for item in self._available_capability_catalog(
                include_memory=include_memory,
                include_cross_session=include_cross_session,
            )
        ]

    def _available_skill_catalog(self) -> list[dict[str, Any]]:
        self.skill_registry.refresh()
        return [skill.to_catalog_dict() for skill in self.skill_registry.list_skills()]

    def _available_capability_catalog(
        self,
        *,
        include_memory: bool,
        include_cross_session: bool = False,
    ) -> list[dict[str, Any]]:
        capabilities: list[dict[str, Any]] = []
        for capability in self.capability_registry.list_capabilities():
            if not capability.enabled:
                continue
            if capability.name == "memory_search" and not include_memory:
                continue
            if capability.name == "cross_session_search" and (
                not include_memory or not include_cross_session
            ):
                continue
            if (
                capability.kind == "external_mcp"
                and not bool(capability.metadata.get("planner_exposed", False))
            ):
                continue
            capabilities.append(self._build_route_capability_descriptor(capability))
        return capabilities

    @staticmethod
    def _build_route_capability_descriptor(
        capability: CapabilityDefinition,
    ) -> dict[str, Any]:
        input_schema = capability.input_schema if isinstance(capability.input_schema, dict) else {}
        properties = (
            input_schema.get("properties")
            if isinstance(input_schema.get("properties"), dict)
            else {}
        )
        required_args = (
            input_schema.get("required")
            if isinstance(input_schema.get("required"), list)
            else []
        )
        descriptor = {
            "name": capability.name,
            "description": capability.description,
            "tags": list(capability.tags),
            "kind": capability.kind,
            "executor": capability.executor,
            "arg_names": sorted(
                str(key)
                for key in properties.keys()
                if isinstance(key, str) and key.strip()
            ),
            "required_args": [
                str(item) for item in required_args if isinstance(item, str) and item.strip()
            ],
        }
        server = capability.metadata.get("server")
        if isinstance(server, str) and server.strip():
            descriptor["server"] = server.strip()
        if "planner_exposed" in capability.metadata:
            descriptor["planner_exposed"] = bool(capability.metadata["planner_exposed"])
        return descriptor

    def _available_tool_names(
        self,
        *,
        include_memory: bool,
        include_cross_session: bool = False,
    ) -> list[str]:
        return self._available_capability_names(
            include_memory=include_memory,
            include_cross_session=include_cross_session,
        )

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [
            capability.to_public_dict()
            for capability in self.capability_registry.list_capabilities()
            if capability.enabled
        ]

    def list_skills(self) -> list[dict[str, Any]]:
        return self._available_skill_catalog()

    def get_capability(self, name: str) -> dict[str, Any] | None:
        capability = self.capability_registry.get(name)
        if capability is None or not capability.enabled:
            return None
        return capability.to_public_dict()

    def list_tools(self) -> list[dict[str, Any]]:
        return self.list_capabilities()

    def _require_enabled_capability(self, capability_name: str):
        capability = self.capability_registry.get(capability_name)
        if capability is None:
            raise LookupError(f"Capability is not registered: {capability_name}")
        if not capability.enabled:
            raise LookupError(f"Capability is disabled: {capability_name}")
        return capability

    @staticmethod
    def _build_capability_invocation_metadata(
        *,
        context: AgentRunContext,
        capability,
    ) -> dict[str, Any]:
        return {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "workspace": context.workspace,
            "domain_schema": context.domain_schema,
            "use_memory": context.use_memory,
            "kind": capability.kind,
            "executor": capability.executor,
        }

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
        prefix = AgentCore._build_answer_annotation_prefix(metadata)
        if prefix and prefix not in answer:
            return f"{prefix}{answer}".strip()
        return answer

    @staticmethod
    def _build_answer_annotation_prefix(metadata: dict[str, Any]) -> str:
        if metadata.get("freshness_action") == "user_correction_refresh":
            return "Refreshed the graph using your correction feedback.\n\n"
        return ""
