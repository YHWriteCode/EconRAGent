from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from lightrag_fork import LightRAG

from kg_agent.agent.builtin_tools import build_default_tool_registry
from kg_agent.agent.path_explainer import PathExplainer
from kg_agent.agent.prompts import build_final_answer_prompt
from kg_agent.agent.route_judge import RouteDecision, RouteJudge
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import AgentLLMClient, KGAgentConfig
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.memory.user_profile import UserProfileStore


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


RagProvider = Callable[[str], LightRAG | Awaitable[LightRAG]]


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

    async def preview_route(
        self,
        *,
        query: str,
        session_id: str,
        user_id: str | None = None,
        use_memory: bool = True,
    ) -> RouteDecision:
        history = []
        if use_memory and self.config.tool_config.enable_memory:
            history = await self.conversation_memory.get_recent_history(
                session_id, self.config.runtime.memory_window_turns
            )
        available_tools = self._available_tool_names(include_memory=use_memory)
        profile = await self.user_profile_store.get_profile(user_id)
        return await self.route_judge.plan(
            query=query,
            session_context={"history": history},
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
        session_history = await self._load_history(context)
        user_profile = await self.user_profile_store.get_profile(user_id)
        route = await self.route_judge.plan(
            query=context.query,
            session_context={"history": session_history},
            user_profile=user_profile,
            available_tools=self._available_tool_names(include_memory=context.use_memory),
        )

        tool_calls: list[dict[str, Any]] = []
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
                session_history=session_history,
                user_profile=user_profile,
                tool_args=tool_plan.args,
            )
            result = await self.tool_registry.execute(
                tool_plan.tool,
                **tool_kwargs,
            )
            tool_calls.append(
                {
                    "tool": tool_plan.tool,
                    "success": result.success,
                    "optional": tool_plan.optional,
                    "summary": result.summary(),
                    "error": result.error,
                    "metadata": result.metadata,
                    "data": result.data if debug else self._prune_tool_data(result.data),
                }
            )
            graph_paths, evidence_chunks = self._accumulate_explanation_inputs(
                result=result,
                current_graph_paths=graph_paths,
                current_evidence=evidence_chunks,
            )

            if self._should_stop_early(
                route=route,
                tool_calls=tool_calls,
                remaining=route.tool_sequence[index + 1 : execution_limit],
            ):
                break

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
            tool_calls=tool_calls,
            path_explanation=path_explanation_dict,
            conversation_history=session_history,
        )

        if context.use_memory and self.config.tool_config.enable_memory:
            await self._persist_memory(context, answer)

        metadata = {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "workspace": context.workspace,
            "domain_schema": context.domain_schema,
            "stream_requested": context.stream,
            "streaming_supported": False,
        }
        return AgentResponse(
            answer=answer,
            route=asdict(route),
            tool_calls=tool_calls,
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

    async def _load_history(self, context: AgentRunContext) -> list[dict[str, str]]:
        if not context.use_memory or not self.config.tool_config.enable_memory:
            return []
        return await self.conversation_memory.get_recent_history(
            context.session_id, self.config.runtime.memory_window_turns
        )

    async def _persist_memory(self, context: AgentRunContext, answer: str) -> None:
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
            metadata={"workspace": context.workspace},
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

    def _build_tool_execution_kwargs(
        self,
        *,
        context: AgentRunContext,
        rag: LightRAG,
        session_history: list[dict[str, str]],
        user_profile: dict[str, Any] | None,
        tool_args: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base_kwargs = {
            "query": context.query,
            "rag": rag,
            "session_id": context.session_id,
            "user_id": context.user_id,
            "session_context": {"history": session_history},
            "user_profile": user_profile,
            "domain_schema": context.domain_schema,
            "memory_store": self.conversation_memory,
        }
        if not tool_args:
            return base_kwargs

        # Keep framework-managed context stable even if an LLM-generated route
        # payload repeats these keys inside tool args.
        reserved_keys = set(base_kwargs)
        sanitized_tool_args = {
            key: value for key, value in tool_args.items() if key not in reserved_keys
        }
        return {**base_kwargs, **sanitized_tool_args}

    @staticmethod
    def _fallback_answer(
        query: str,
        route: RouteDecision,
        tool_calls: list[dict[str, Any]],
        path_explanation: dict[str, Any] | None,
    ) -> str:
        lines = [
            f"问题：{query}",
            f"路由策略：{route.strategy}",
            f"路由原因：{route.reason}",
        ]
        if tool_calls:
            lines.append("工具执行结果：")
            for item in tool_calls:
                status = "成功" if item["success"] else "失败"
                lines.append(f"- {item['tool']}：{status}，{item['summary']}")
        if path_explanation and path_explanation.get("enabled"):
            lines.append("路径解释：")
            lines.append(path_explanation.get("final_explanation", ""))
        return "\n".join(lines)

    @staticmethod
    def _prune_tool_data(data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        summary: dict[str, Any] = {"summary": data.get("summary")}
        if "status" in data:
            summary["status"] = data["status"]
        if "data" in data and isinstance(data["data"], dict):
            summary["counts"] = {
                key: len(value)
                for key, value in data["data"].items()
                if isinstance(value, list)
            }
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
        if route.need_path_explanation:
            return False
        if not tool_calls:
            return False
        last_call = tool_calls[-1]
        if not last_call["success"]:
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
