from dataclasses import dataclass
from pathlib import Path

import pytest

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.path_explainer import PathExplanation
from kg_agent.agent.route_judge import RouteDecision, ToolCallPlan
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import (
    AgentModelConfig,
    AgentRuntimeConfig,
    KGAgentConfig,
    ToolConfig,
)
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.skills import (
    SkillCommandPlanner,
    SkillExecutor,
    SkillLoader,
    SkillPlan,
    SkillRegistry,
)
from kg_agent.tools.base import ToolDefinition, ToolResult


class _FakeRAG:
    workspace = ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


async def _echo_tool(query: str, custom: str | None = None, **kwargs):
    return ToolResult(
        tool_name="echo",
        success=True,
        data={"query": query, "custom": custom},
    )


class _TrackingCrossSessionStore:
    def __init__(self):
        self.indexed_messages = []

    async def search(self, *args, **kwargs):
        return []

    async def index_message(self, message):
        self.indexed_messages.append(message)


@dataclass
class _CapturingRouteJudge:
    route: RouteDecision
    seen_session_context: dict | None = None
    seen_attachments: list[dict] | None = None

    async def plan(self, **kwargs):
        self.seen_session_context = kwargs.get("session_context")
        attachments = kwargs.get("attachments")
        self.seen_attachments = list(attachments) if isinstance(attachments, list) else None
        return self.route


class _StubSkillExecutor:
    def __init__(self):
        self.calls = []
        self.status_calls = []
        self.cancel_calls = []
        self.logs_calls = []
        self.artifact_calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=f"skill:{kwargs['skill_name']}",
            success=True,
            data={
                "run_status": "planned",
                "status": "planned",
                "skill_name": kwargs["skill_name"],
                "goal": kwargs["goal"],
                "workspace": kwargs.get("workspace"),
                "command_plan": {"mode": "inferred"},
                "summary": f"Prepared skill {kwargs['skill_name']}",
            },
            metadata={"executor": "skill", "skill_name": kwargs["skill_name"]},
        )

    async def get_run_status(self, *, run_id: str):
        self.status_calls.append(run_id)
        return {
            "run_id": run_id,
            "skill_name": "example-skill",
            "run_status": "completed",
            "status": "completed",
            "success": True,
            "command_plan": {"mode": "explicit"},
        }

    async def cancel_skill_run(self, *, run_id: str):
        self.cancel_calls.append(run_id)
        return {
            "run_id": run_id,
            "skill_name": "example-skill",
            "run_status": "failed",
            "status": "failed",
            "success": False,
            "failure_reason": "cancelled",
            "cancel_requested": True,
            "command_plan": {"mode": "explicit"},
        }

    async def get_run_logs(self, *, run_id: str):
        self.logs_calls.append(run_id)
        return {
            "run_id": run_id,
            "run_status": "completed",
            "status": "completed",
            "stdout": "",
            "stderr": "",
        }

    async def get_run_artifacts(self, *, run_id: str):
        self.artifact_calls.append(run_id)
        return {
            "run_id": run_id,
            "run_status": "completed",
            "status": "completed",
            "artifacts": [],
        }


class _FailedBootstrapSkillExecutor(_StubSkillExecutor):
    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=f"skill:{kwargs['skill_name']}",
            success=False,
            data={
                "run_status": "failed",
                "status": "failed",
                "skill_name": kwargs["skill_name"],
                "goal": kwargs["goal"],
                "workspace": "/workspace/runs/pptx-test",
                "failure_reason": "bootstrap_failed",
                "auto_inferred_constraints": {
                    "language": "zh",
                    "slide_count": "10",
                    "theme": "Midnight Executive",
                    "style": "modern",
                    "dry_run": False,
                    "plan_only": False,
                },
                "command_plan": {"mode": "generated_script"},
                "logs_preview": {
                    "stdout": "[bootstrap 1/2] npm install -g pptxgenjs\n",
                    "stderr": "/bin/sh: 1: npm: not found\n",
                },
                "summary": "Loaded artifacts for failed skill run",
            },
            metadata={"executor": "skill", "skill_name": kwargs["skill_name"]},
        )


class _StubPlannerLLM:
    def __init__(self, payloads):
        if isinstance(payloads, list):
            self.payloads = [dict(item) for item in payloads]
        else:
            self.payloads = [dict(payloads)]

    def is_available(self):
        return True

    async def complete_json(self, **kwargs):
        if not self.payloads:
            raise RuntimeError("No stub payload remaining")
        return self.payloads.pop(0)


class _StubUploadStore:
    def __init__(self, *, unsupported_multimodal: bool = False):
        self.unsupported_multimodal = unsupported_multimodal
        self.seen_attachment_ids: list[str] = []

    async def build_attachment_context(self, upload_ids):
        self.seen_attachment_ids = list(upload_ids)
        return {
            "attachments": [
                {
                    "upload_id": upload_id,
                    "filename": "brief.txt",
                    "stored_path": f"/tmp/{upload_id}.txt",
                    "kind": "text",
                    "status": "ready",
                }
                for upload_id in upload_ids
            ],
            "prompt": "\n\n[附件上下文]\n附件摘要：新能源供应链月报。",
            "unsupported_multimodal": self.unsupported_multimodal,
        }


@pytest.mark.asyncio
async def test_agent_core_chat_ignores_duplicate_reserved_tool_args():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo test tool",
            input_schema={},
            handler=_echo_tool,
        )
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="test_echo",
        tool_sequence=[
            ToolCallPlan(
                tool="echo",
                args={"query": "shadow-query", "session_id": "shadow-session", "custom": "ok"},
            )
        ],
        reason="test route",
        max_iterations=1,
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=registry,
        route_judge=_StubRouteJudge(route=route),
    )

    response = await agent.chat(
        query="real-query",
        session_id="real-session",
        debug=True,
        use_memory=False,
    )

    assert response.tool_calls[0]["success"] is True
    assert response.tool_calls[0]["data"]["query"] == "real-query"
    assert response.tool_calls[0]["data"]["custom"] == "ok"


@pytest.mark.asyncio
async def test_agent_core_invoke_capability_executes_native_capability_directly():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo test tool",
            input_schema={},
            handler=_echo_tool,
        )
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=registry,
    )

    response = await agent.invoke_capability(
        capability_name="echo",
        query="real-query",
        session_id="capability-session",
        use_memory=False,
        args={"query": "shadow-query", "custom": "ok"},
    )

    assert response.capability["name"] == "echo"
    assert response.result["tool"] == "echo"
    assert response.result["success"] is True
    assert response.result["data"]["query"] == "real-query"
    assert response.result["data"]["custom"] == "ok"
    assert response.metadata["executor"] == "tool_registry"
    assert response.metadata["use_memory"] is False


@pytest.mark.asyncio
async def test_agent_core_invoke_capability_rejects_unknown_capability():
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(agent_model=AgentModelConfig(provider="disabled")),
    )

    with pytest.raises(LookupError, match="Capability is not registered: missing_skill"):
        await agent.invoke_capability(
            capability_name="missing_skill",
            session_id="capability-session",
        )


@pytest.mark.asyncio
async def test_agent_core_chat_stream_yields_meta_delta_done_and_persists_memory():
    class _StreamingLLM:
        def is_available(self):
            return True

        async def stream_text(self, **kwargs):
            yield "Hello"
            yield " world"

        async def complete_text(self, **kwargs):
            return "Hello world"

    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="simple_answer_no_tool",
        tool_sequence=[],
        reason="stream test",
        max_iterations=1,
    )
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=True),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    memory = ConversationMemoryStore()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        conversation_memory=memory,
    )
    agent.llm_client = _StreamingLLM()

    events = [
        event
        async for event in agent.chat_stream(
            query="stream hello",
            session_id="stream-session",
            user_id="user-stream",
            use_memory=True,
        )
    ]

    assert [item["type"] for item in events] == [
        "meta",
        "route",
        "answer_start",
        "delta",
        "delta",
        "done",
    ]
    assert events[-1]["answer"] == "Hello world"

    history = await memory.get_recent_history("stream-session", turns=2)
    assert history[-1]["content"] == "Hello world"


@pytest.mark.asyncio
async def test_agent_core_chat_stream_emits_tool_and_path_events():
    class _StreamingLLM:
        def is_available(self):
            return True

        async def stream_text(self, **kwargs):
            yield "Explained"
            yield " answer"

        async def complete_text(self, **kwargs):
            return "Explained answer"

    class _StubPathExplainer:
        async def explain(self, **kwargs):
            return PathExplanation(
                enabled=True,
                question_type="relation_explanation",
                core_entities=["Policy", "BYD"],
                paths=[],
                final_explanation="Policy drives BYD through demand.",
                uncertainty=None,
            )

    async def _hybrid_tool(**kwargs):
        return ToolResult(
            tool_name="kg_hybrid_search",
            success=True,
            data={
                "data": {
                    "chunks": [
                        {
                            "content": "Policy support increased EV demand and helped BYD.",
                        }
                    ]
                }
            },
        )

    async def _trace_tool(**kwargs):
        return ToolResult(
            tool_name="graph_relation_trace",
            success=True,
            data={
                "paths": [
                    {
                        "path_text": "Policy -> EV demand -> BYD",
                        "nodes": [{"id": "Policy"}, {"id": "BYD"}],
                        "edges": [],
                    }
                ]
            },
        )

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="kg_hybrid_search",
            description="Hybrid search",
            input_schema={},
            handler=_hybrid_tool,
        )
    )
    registry.register(
        ToolDefinition(
            name="graph_relation_trace",
            description="Relation trace",
            input_schema={},
            handler=_trace_tool,
        )
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=True,
        strategy="kg_hybrid_first_then_graph_trace",
        tool_sequence=[
            ToolCallPlan(tool="kg_hybrid_search"),
            ToolCallPlan(tool="graph_relation_trace"),
        ],
        reason="stream stages test",
        max_iterations=2,
    )
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=registry,
        route_judge=_StubRouteJudge(route=route),
        path_explainer=_StubPathExplainer(),
    )
    agent.llm_client = _StreamingLLM()

    events = [
        event
        async for event in agent.chat_stream(
            query="Why does policy affect BYD?",
            session_id="stream-tools",
            use_memory=False,
        )
    ]

    event_types = [item["type"] for item in events]
    assert event_types == [
        "meta",
        "route",
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "path_explanation_start",
        "path_explanation",
        "answer_start",
        "delta",
        "delta",
        "done",
    ]
    assert events[2]["tool"] == "kg_hybrid_search"
    assert events[3]["tool_call"]["tool"] == "kg_hybrid_search"
    assert events[5]["tool_call"]["tool"] == "graph_relation_trace"
    assert events[7]["path_explanation"]["final_explanation"] == (
        "Policy drives BYD through demand."
    )
    assert events[-1]["answer"] == "Explained answer"


@pytest.mark.asyncio
async def test_agent_core_persists_messages_into_cross_session_store():
    class _SilentLLM:
        def is_available(self):
            return False

    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="simple_answer_no_tool",
        tool_sequence=[],
        reason="cross-session persist",
        max_iterations=1,
    )
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=True),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    memory = ConversationMemoryStore()
    cross_session_store = _TrackingCrossSessionStore()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        conversation_memory=memory,
        cross_session_store=cross_session_store,
    )
    agent.llm_client = _SilentLLM()

    response = await agent.chat(
        query="Remember this supplier note",
        session_id="cross-session",
        user_id="user-9",
        use_memory=True,
    )

    assert response.answer
    assert len(cross_session_store.indexed_messages) == 2
    assert cross_session_store.indexed_messages[0].role == "user"
    assert cross_session_store.indexed_messages[1].role == "assistant"


@pytest.mark.asyncio
async def test_agent_core_chat_passes_workspace_to_rag_provider():
    seen_workspaces: list[str] = []
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="simple_answer_no_tool",
        tool_sequence=[],
        reason="no tools needed",
        max_iterations=1,
    )

    async def rag_provider(workspace: str):
        seen_workspaces.append(workspace)
        rag = _FakeRAG()
        rag.workspace = workspace
        return rag

    agent = AgentCore(
        rag_provider=rag_provider,
        config=config,
        route_judge=_StubRouteJudge(route=route),
    )

    response = await agent.chat(
        query="hello",
        session_id="session-workspace",
        workspace="ws-dynamic",
        use_memory=False,
    )

    assert response.metadata["workspace"] == "ws-dynamic"
    assert seen_workspaces == ["ws-dynamic"]


@pytest.mark.asyncio
async def test_agent_core_chat_all_workspace_fans_out_retrieval():
    seen_workspaces: list[str] = []
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="factual_qa",
        tool_sequence=[ToolCallPlan(tool="kg_hybrid_search")],
        reason="search knowledge",
        max_iterations=1,
    )

    class _SearchRAG:
        def __init__(self, workspace: str):
            self.workspace = workspace

        async def aquery_data(self, query, param=None):
            if self.workspace == "macro":
                return {
                    "status": "success",
                    "data": {
                        "entities": [{"entity": "规模经济"}],
                        "relationships": [],
                        "chunks": [
                            {
                                "content": "规模经济描述了产量扩大时平均成本下降。",
                                "file_path": "macro.md",
                            }
                        ],
                        "references": [],
                    },
                }
            return {
                "status": "no_result",
                "message": "No query context could be built.",
                "data": {
                    "entities": [],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
            }

    async def rag_provider(workspace: str):
        seen_workspaces.append(workspace)
        return _SearchRAG(workspace)

    async def workspace_lister():
        return ["empty-default", "macro"]

    agent = AgentCore(
        rag_provider=rag_provider,
        workspace_lister=workspace_lister,
        config=config,
        route_judge=_StubRouteJudge(route=route),
    )

    response = await agent.chat(
        query="规模经济是什么？",
        session_id="session-all-workspaces",
        workspace="all",
        use_memory=False,
        debug=True,
    )

    tool_call = response.tool_calls[0]
    payload = tool_call["data"]["data"]
    assert response.metadata["workspace"] == "all"
    assert seen_workspaces == ["empty-default", "macro"]
    assert tool_call["success"] is True
    assert payload["entities"][0]["workspace_id"] == "macro"
    assert payload["chunks"][0]["workspace_id"] == "macro"


@pytest.mark.asyncio
async def test_agent_core_preview_route_uses_smart_memory_window():
    memory = ConversationMemoryStore()
    await memory.append_message(
        "session-memory",
        "user",
        "Recall the supplier battery issue from last month.",
    )
    await memory.append_message(
        "session-memory",
        "assistant",
        "Supplier Alpha slipped because logistics stalled.",
    )
    await memory.append_message("session-memory", "user", "Thanks")
    await memory.append_message("session-memory", "assistant", "Acknowledged")
    await memory.append_message("session-memory", "user", "We are now discussing lunch")
    await memory.append_message("session-memory", "assistant", "Lunch plan is undecided.")

    route = RouteDecision(
        need_tools=False,
        need_memory=True,
        need_web_search=False,
        need_path_explanation=False,
        strategy="memory_preview",
        tool_sequence=[],
        reason="capture session context",
        max_iterations=1,
    )
    capturing_judge = _CapturingRouteJudge(route=route)
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=True),
        runtime=AgentRuntimeConfig(
            default_workspace="",
            max_iterations=3,
            memory_window_turns=2,
            memory_min_recent_turns=1,
            memory_max_context_tokens=80,
        ),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=capturing_judge,
        conversation_memory=memory,
    )

    await agent.preview_route(
        query="supplier logistics battery update",
        session_id="session-memory",
        use_memory=True,
    )

    history = capturing_judge.seen_session_context["history"]
    contents = [item["content"] for item in history]
    assert "Recall the supplier battery issue from last month." in contents
    assert "Supplier Alpha slipped because logistics stalled." in contents
    assert "We are now discussing lunch" in contents
    assert "Lunch plan is undecided." in contents
    assert "Acknowledged" not in contents


@pytest.mark.asyncio
async def test_agent_core_chat_applies_query_mode_web_search_and_attachment_overrides():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    registry = ToolRegistry()
    observed_queries: dict[str, str] = {}

    async def _web_search_tool(query: str, **kwargs):
        observed_queries["web_search"] = query
        return ToolResult(
            tool_name="web_search",
            success=True,
            data={"pages": [], "urls": []},
            metadata={"source": "stub"},
        )

    async def _naive_search_tool(query: str, **kwargs):
        observed_queries["kg_naive_search"] = query
        return ToolResult(
            tool_name="kg_naive_search",
            success=True,
            data={"data": {"chunks": []}, "status": "success"},
            metadata={"mode": "naive"},
        )

    async def _hybrid_search_tool(query: str, mode: str = "hybrid", **kwargs):
        observed_queries["kg_hybrid_search"] = f"{mode}:{query}"
        return ToolResult(
            tool_name="kg_hybrid_search",
            success=True,
            data={"data": {"chunks": []}, "status": "success"},
            metadata={"mode": mode},
        )

    registry.register(
        ToolDefinition(
            name="web_search",
            description="Stub web search",
            input_schema={},
            handler=_web_search_tool,
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_naive_search",
            description="Stub naive search",
            input_schema={},
            handler=_naive_search_tool,
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_hybrid_search",
            description="Stub hybrid search",
            input_schema={},
            handler=_hybrid_search_tool,
        )
    )

    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="planner_default",
        tool_sequence=[ToolCallPlan(tool="kg_hybrid_search")],
        reason="planner defaulted to hybrid retrieval",
        max_iterations=2,
    )
    upload_store = _StubUploadStore(unsupported_multimodal=True)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=registry,
        route_judge=_StubRouteJudge(route=route),
        upload_store=upload_store,
    )

    response = await agent.chat(
        query="总结附件中的重点",
        session_id="override-session",
        use_memory=False,
        query_mode="naive",
        force_web_search=True,
        attachment_ids=["upload-1"],
    )

    assert upload_store.seen_attachment_ids == ["upload-1"]
    assert [item["tool"] for item in response.tool_calls] == [
        "web_search",
        "kg_naive_search",
    ]
    assert "[附件上下文]" in observed_queries["web_search"]
    assert "新能源供应链月报" in observed_queries["kg_naive_search"]
    assert response.route["strategy"] == "manual_route_override"
    assert response.metadata["requested_query_mode"] == "naive"
    assert response.metadata["effective_query_mode"] == "naive"
    assert response.metadata["web_search_forced"] is True
    assert response.metadata["attachment_ids"] == ["upload-1"]
    assert response.metadata["unsupported_multimodal"] is True
    assert response.metadata["attachments"][0]["filename"] == "brief.txt"
    assert response.metadata["attachments"][0]["stored_path"] == "/tmp/upload-1.txt"


@pytest.mark.asyncio
async def test_agent_core_query_mode_override_does_not_suppress_skill_route():
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="example-skill",
            goal="Use example-skill to create a report",
            reason="Matched local skill",
        ),
    )
    skill_executor = _StubSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query="run a report skill",
        session_id="skill-route-with-query-mode",
        use_memory=False,
        debug=True,
        query_mode="hybrid",
    )

    assert [item["tool"] for item in response.tool_calls] == ["skill:example-skill"]
    assert response.route["strategy"] == "skill_request"
    assert response.metadata["requested_query_mode"] == "hybrid"
    assert response.metadata["effective_query_mode"] is None
    assert skill_executor.calls[0]["skill_name"] == "example-skill"


@pytest.mark.asyncio
async def test_agent_core_dispatches_skill_plan_to_skill_executor():
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="example-skill",
            goal="Use example-skill to create a report",
            reason="Matched local skill",
        ),
    )
    skill_executor = _StubSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query="run a report skill",
        session_id="skill-workflow-session",
        use_memory=False,
        debug=True,
    )

    assert [item["tool"] for item in response.tool_calls] == ["skill:example-skill"]
    assert skill_executor.calls[0]["skill_name"] == "example-skill"
    assert skill_executor.calls[0]["goal"] == "Use example-skill to create a report"
    assert skill_executor.calls[0]["workspace"] is None


@pytest.mark.asyncio
async def test_agent_core_passes_structured_attachments_to_route_judge():
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="attachment_context_answer",
        tool_sequence=[],
        reason="attachment context route",
        max_iterations=1,
    )
    capturing_judge = _CapturingRouteJudge(route=route)
    upload_store = _StubUploadStore()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=capturing_judge,
        upload_store=upload_store,
    )

    await agent.chat(
        query="读取这个文件",
        session_id="attachment-route-session",
        use_memory=False,
        attachment_ids=["upload-1"],
    )

    assert capturing_judge.seen_attachments is not None
    assert capturing_judge.seen_attachments[0]["upload_id"] == "upload-1"
    assert capturing_judge.seen_attachments[0]["stored_path"] == "/tmp/upload-1.txt"


@pytest.mark.asyncio
async def test_agent_core_auto_binds_single_attachment_to_skill_constraints():
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="Matched local skill",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pdf",
            goal="Use pdf skill to read the attachment",
            reason="Matched local skill",
            constraints={},
        ),
    )
    skill_executor = _StubSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
        upload_store=_StubUploadStore(),
    )

    await agent.chat(
        query="读取这个文件",
        session_id="attachment-skill-bind-session",
        use_memory=False,
        attachment_ids=["upload-1"],
    )

    assert skill_executor.calls[0]["constraints"]["input_path"] == "/tmp/upload-1.txt"
    assert skill_executor.calls[0]["constraints"]["source_path"] == "/tmp/upload-1.txt"
    assert skill_executor.calls[0]["constraints"]["upload_id"] == "upload-1"


@pytest.mark.asyncio
async def test_agent_core_waits_for_running_skill_to_reach_terminal_status():
    class _RunningSkillExecutor(_StubSkillExecutor):
        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            return ToolResult(
                tool_name=f"skill:{kwargs['skill_name']}",
                success=True,
                data={
                    "run_id": "run-99",
                    "run_status": "running",
                    "status": "running",
                    "skill_name": kwargs["skill_name"],
                    "goal": kwargs["goal"],
                    "workspace": kwargs.get("workspace"),
                    "summary": "Skill enqueued",
                    "command_plan": {"mode": "explicit"},
                },
                metadata={"executor": "skill", "skill_name": kwargs["skill_name"]},
            )

        async def get_run_status(self, *, run_id: str):
            self.status_calls.append(run_id)
            return {
                "run_id": run_id,
                "run_status": "completed",
                "status": "completed",
                "success": True,
                "summary": "Skill finished",
                "workspace": "host-run-99",
                "command_plan": {"mode": "explicit"},
            }

        async def get_run_logs(self, *, run_id: str):
            self.logs_calls.append(run_id)
            return {
                "run_id": run_id,
                "run_status": "completed",
                "status": "completed",
                "stdout": "report generated",
                "stderr": "",
            }

        async def get_run_artifacts(self, *, run_id: str):
            self.artifact_calls.append(run_id)
            return {
                "run_id": run_id,
                "run_status": "completed",
                "status": "completed",
                "workspace": "host-run-99",
                "artifacts": [{"path": "output/report.md", "size_bytes": 128}],
            }

    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="example-skill",
            goal="Use example-skill to create a report",
            reason="Matched local skill",
        ),
    )
    skill_executor = _RunningSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query="run a report skill",
        session_id="skill-terminal-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["data"]["run_status"] == "completed"
    assert response.tool_calls[0]["data"]["status"] == "completed"
    assert response.tool_calls[0]["data"]["workspace"] == "host-run-99"


@pytest.mark.asyncio
async def test_agent_core_reports_running_skill_without_fabricating_failure_answer():
    class _StillRunningSkillExecutor(_StubSkillExecutor):
        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            return ToolResult(
                tool_name=f"skill:{kwargs['skill_name']}",
                success=True,
                data={
                    "run_id": "run-123",
                    "run_status": "running",
                    "status": "running",
                    "skill_name": kwargs["skill_name"],
                    "goal": kwargs["goal"],
                    "workspace": kwargs.get("workspace"),
                    "summary": "Running free-shell repair loop for skill 'pptx'.",
                    "shell_mode_effective": "free_shell",
                    "command_plan": {"mode": "generated_script"},
                },
                metadata={"executor": "skill", "skill_name": kwargs["skill_name"]},
            )

        async def get_run_status(self, *, run_id: str):
            self.status_calls.append(run_id)
            raise RuntimeError("status polling unavailable")

    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pptx",
            goal="Use skill 'pptx' to fulfill the user request: 生成“科幻小说起源发展”的PPT文件",
            reason="Matched local skill",
        ),
    )
    skill_executor = _StillRunningSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query="生成“科幻小说起源发展”的PPT文件",
        session_id="skill-running-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["data"]["run_status"] == "running"
    assert "仍在运行中" in response.answer
    assert "当前还没有最终结果" in response.answer
    assert "技术性阻塞" not in response.answer
    assert skill_executor.status_calls == ["run-123"]


@pytest.mark.asyncio
async def test_agent_core_exact_ppt_request_auto_escalates_skill_instead_of_generic_manual_stop():
    llm = _StubPlannerLLM(
        [
            {
                "constraints": {
                    "topic": "生成式人工智能起源发展",
                    "title": "生成式人工智能起源发展",
                    "output_format": "pptx",
                    "slide_count": "10",
                },
                "reason": "Promoted the request into direct artifact defaults.",
                "confidence": "high",
            },
            {
                "mode": "generated_script",
                "command": None,
                "entrypoint": ".skill_generated/main.py",
                "cli_args": [],
                "generated_files": [
                    {
                        "path": ".skill_generated/main.py",
                        "content": "print('build pptx deck')\n",
                        "description": "Generated PPTX workflow entrypoint.",
                    }
                ],
                "rationale": "Use the PPTX docs to assemble a generated deck workflow.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            },
        ]
    )
    skill_registry = SkillRegistry(Path("skills"))
    skill_loader = SkillLoader(skill_registry)
    skill_executor = SkillExecutor(
        registry=skill_registry,
        loader=skill_loader,
        command_planner=SkillCommandPlanner(llm_client=llm),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query='请帮我做一个关于“生成式人工智能起源发展”的PPT',
        session_id="ppt-auto-escalation-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["tool"] == "skill:pptx"
    assert response.tool_calls[0]["success"] is True
    assert response.tool_calls[0]["data"]["run_status"] == "planned"
    assert response.tool_calls[0]["data"]["command_plan"]["mode"] == "generated_script"
    assert response.tool_calls[0]["data"]["shell_mode_effective"] == "free_shell"
    assert response.tool_calls[0]["data"]["shell_mode_escalated"] is True
    assert (
        response.tool_calls[0]["data"]["command_plan"]["constraints"]["topic"]
        == "生成式人工智能起源发展"
    )
    assert response.tool_calls[0]["summary"] != "Skill 'pptx' needs more execution detail before it can run."


@pytest.mark.asyncio
async def test_agent_core_reports_technical_skill_blockers_without_asking_for_more_ppt_details():
    skill_registry = SkillRegistry(Path("skills"))
    skill_loader = SkillLoader(skill_registry)
    skill_executor = SkillExecutor(
        registry=skill_registry,
        loader=skill_loader,
        command_planner=SkillCommandPlanner(),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query='请帮我做一个“科幻小说起源发展”的PPT',
        session_id="ppt-technical-blocker-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["tool"] == "skill:pptx"
    assert response.tool_calls[0]["success"] is False
    assert response.tool_calls[0]["data"]["run_status"] == "manual_required"
    assert response.tool_calls[0]["data"]["manual_required_kind"] == "technical_blocked"
    assert "技术性阻塞" in response.answer
    assert "更多结构化参数" not in response.answer
    assert "页数" not in response.answer
    assert "稍后重试" not in response.answer
    assert "更具体的步骤" not in response.answer
    assert "dry_run" not in response.answer
    assert "plan_only" not in response.answer


@pytest.mark.asyncio
async def test_agent_core_treats_doc_only_skill_as_completed_advisory_instead_of_technical_blocker():
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="pricing skill request",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pricing",
            goal="Use pricing skill to advise on handmade mugwort rice cakes pricing.",
            reason="Matched pricing skill.",
            constraints={},
        ),
    )
    skill_registry = SkillRegistry(Path("skills"))
    skill_loader = SkillLoader(skill_registry)
    skill_executor = SkillExecutor(
        registry=skill_registry,
        loader=skill_loader,
        command_planner=SkillCommandPlanner(),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        route_judge=_StubRouteJudge(route=route),
        skill_executor=skill_executor,
    )

    response = await agent.chat(
        query="使用定价技能，我家从野外采摘了不少艾草，并配合糯米手工制作了不少米果，我该怎么定价好些？",
        session_id="pricing-doc-only-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["tool"] == "skill:pricing"
    assert response.tool_calls[0]["success"] is True
    assert response.tool_calls[0]["data"]["run_status"] == "completed"
    assert response.tool_calls[0]["data"]["execution_mode"] == "doc_advisory"
    assert response.tool_calls[0]["data"]["advisory_mode"] == "doc_only"
    assert "技术性阻塞" not in response.answer
    assert "environment_not_prepared" not in response.answer


@pytest.mark.asyncio
async def test_agent_core_reports_failed_skill_bootstrap_as_system_blocker_without_alternatives():
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_executor=_FailedBootstrapSkillExecutor(),
    )

    response = await agent.chat(
        query='生成“科幻小说起源发展”的PPT文件',
        session_id="ppt-bootstrap-failure-session",
        use_memory=False,
        debug=True,
    )

    assert response.tool_calls[0]["tool"] == "skill:pptx"
    assert response.tool_calls[0]["success"] is False
    assert response.tool_calls[0]["data"]["run_status"] == "failed"
    assert response.tool_calls[0]["data"]["failure_reason"] == "bootstrap_failed"
    assert "系统侧技术性阻塞" in response.answer
    assert "bootstrap_failed" in response.answer
    assert "npm: not found" in response.answer
    assert "None |" not in response.answer
    assert "稍后重试" not in response.answer
    assert "结构化内容大纲" not in response.answer
    assert "dry_run" not in response.answer
    assert "plan_only" not in response.answer


@pytest.mark.asyncio
async def test_agent_core_can_answer_agent_metadata_from_catalog_without_tools(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "financial-researching"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: financial-researching\n"
        "description: Analyze stock trends and financial data.\n"
        "tags:\n"
        "  - finance\n"
        "---\n"
        "# financial-researching\n",
        encoding="utf-8",
    )

    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="agent_metadata_answer",
        tool_sequence=[],
        reason="metadata is already available in context",
        max_iterations=1,
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="kg_hybrid_search",
            description="Hybrid retrieval",
            input_schema={},
            handler=_echo_tool,
        )
    )
    skill_registry = SkillRegistry(tmp_path / "skills")
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(
                default_workspace="",
                max_iterations=3,
                skills_dir=str(tmp_path / "skills"),
            ),
        ),
        tool_registry=registry,
        route_judge=_StubRouteJudge(route=route),
        skill_registry=skill_registry,
        skill_loader=SkillLoader(skill_registry),
    )

    response = await agent.chat(
        query="你有哪些工具和技能？",
        session_id="agent-metadata-session",
        use_memory=False,
    )

    assert "kg_hybrid_search" in response.answer
    assert "financial-researching" in response.answer
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_agent_core_invoke_skill_executes_local_skill_directly(tmp_path: Path):
    skill_dir = tmp_path / "skills" / "example-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# example-skill\n\nPrepare a local workflow.\n",
        encoding="utf-8",
    )
    skill_executor = _StubSkillExecutor()
    skill_registry = SkillRegistry(tmp_path / "skills")
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_registry=skill_registry,
        skill_loader=SkillLoader(skill_registry),
        skill_executor=skill_executor,
    )

    response = await agent.invoke_skill(
        skill_name="example-skill",
        session_id="skill-invoke-session",
        goal="Prepare the local skill workflow",
        query="run the skill against this workbook",
        workspace="ops",
        constraints={"format": "xlsx"},
    )

    assert response.skill["name"] == "example-skill"
    assert response.result["tool"] == "skill:example-skill"
    assert response.result["success"] is True
    assert response.result["data"]["goal"] == "Prepare the local skill workflow"
    assert response.metadata["kind"] == "skill"
    assert response.metadata["workspace"] == "ops"
    assert skill_executor.calls[0]["constraints"] == {"format": "xlsx"}


@pytest.mark.asyncio
async def test_agent_core_get_skill_run_status_delegates_to_skill_executor():
    skill_executor = _StubSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_executor=skill_executor,
    )

    status = await agent.get_skill_run_status(run_id="run-42")

    assert skill_executor.status_calls == ["run-42"]
    assert status["run_status"] == "completed"
    assert status["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_core_cancel_skill_run_delegates_to_skill_executor():
    skill_executor = _StubSkillExecutor()
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        skill_executor=skill_executor,
    )

    status = await agent.cancel_skill_run(run_id="run-42")

    assert skill_executor.cancel_calls == ["run-42"]
    assert status["run_status"] == "failed"
    assert status["failure_reason"] == "cancelled"
    assert status["cancel_requested"] is True


def test_agent_core_builds_route_judge_with_configured_prompt_version():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(
            default_workspace="",
            max_iterations=3,
            route_judge_prompt_version="v2",
        ),
    )

    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
    )

    assert agent.route_judge.prompt_version == "v2"
