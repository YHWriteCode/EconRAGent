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
from kg_agent.skills import SkillLoader, SkillPlan, SkillRegistry
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

    async def plan(self, **kwargs):
        self.seen_session_context = kwargs.get("session_context")
        return self.route


class _StubSkillExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return ToolResult(
            tool_name=f"skill:{kwargs['skill_name']}",
            success=True,
            data={
                "status": "prepared",
                "skill_name": kwargs["skill_name"],
                "goal": kwargs["goal"],
                "workspace": kwargs.get("workspace"),
                "summary": f"Prepared skill {kwargs['skill_name']}",
            },
            metadata={"executor": "skill", "skill_name": kwargs["skill_name"]},
        )


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
