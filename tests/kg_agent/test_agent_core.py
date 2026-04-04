from dataclasses import dataclass

import pytest

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.route_judge import RouteDecision, ToolCallPlan
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import (
    AgentModelConfig,
    AgentRuntimeConfig,
    KGAgentConfig,
    ToolConfig,
)
from kg_agent.memory.conversation_memory import ConversationMemoryStore
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


@pytest.mark.asyncio
async def test_agent_core_chat_ignores_duplicate_reserved_tool_args():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False, enable_quant=False),
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
        tool_config=ToolConfig(enable_memory=True, enable_quant=False),
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

    assert [item["type"] for item in events] == ["meta", "delta", "delta", "done"]
    assert events[-1]["answer"] == "Hello world"

    history = await memory.get_recent_history("stream-session", turns=2)
    assert history[-1]["content"] == "Hello world"


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
        tool_config=ToolConfig(enable_memory=True, enable_quant=False),
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
        tool_config=ToolConfig(enable_memory=False, enable_quant=False),
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
