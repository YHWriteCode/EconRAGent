from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.route_judge import RouteDecision, RouteJudge, ToolCallPlan
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import (
    AgentModelConfig,
    AgentRuntimeConfig,
    FreshnessConfig,
    KGAgentConfig,
    ToolConfig,
)
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.tools.base import ToolDefinition, ToolResult
from kg_agent.tools.retrieval_tools import kg_hybrid_search
from lightrag_fork.base import QueryParam
from lightrag_fork.operate import _sort_items_with_freshness_decay
from lightrag_fork.utils import convert_to_user_format


@pytest.mark.asyncio
async def test_conversation_memory_returns_recent_compact_tool_calls():
    store = ConversationMemoryStore()
    await store.append_message("s2", "user", "latest tesla deliveries")
    await store.append_message(
        "s2",
        "assistant",
        "Here is the latest graph-backed answer.",
        metadata={
            "compact_tool_calls": [
                {
                    "tool": "kg_hybrid_search",
                    "success": True,
                    "summary": "Retrieved graph data",
                    "strategy": "freshness_aware_search",
                    "timestamp": "2026-04-02T00:00:00+00:00",
                }
            ]
        },
    )

    tool_calls = await store.get_recent_tool_calls("s2", assistant_turns=1)

    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "kg_hybrid_search"


@pytest.mark.asyncio
async def test_route_judge_realtime_prefers_freshness_aware_search():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="latest tesla deliveries today",
        session_context={"history": [], "recent_tool_calls": []},
        user_profile={},
        available_capabilities=["web_search", "kg_hybrid_search"],
    )

    assert route.strategy == "freshness_aware_search"
    assert [item.tool for item in route.tool_sequence] == [
        "web_search",
        "kg_hybrid_search",
    ]


@pytest.mark.asyncio
async def test_route_judge_correction_requires_recent_kg_tools():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="this is outdated",
        session_context={
            "history": [{"role": "user", "content": "latest tesla deliveries"}],
            "recent_tool_calls": [
                {
                    "tool": "kg_hybrid_search",
                    "success": True,
                    "summary": "Retrieved graph data",
                }
            ],
        },
        user_profile={},
        available_capabilities=["web_search", "kg_hybrid_search"],
    )

    assert route.strategy == "correction_and_refresh"
    assert route.tool_sequence[0].tool == "web_search"
    assert route.tool_sequence[0].args["search_query"] == "latest tesla deliveries"


class _FakeRAG:
    workspace = ""


@dataclass
class _CapturingRouteJudge:
    routes: list[RouteDecision]
    seen_session_contexts: list[dict] = field(default_factory=list)

    async def plan(self, **kwargs):
        self.seen_session_contexts.append(kwargs["session_context"])
        return self.routes.pop(0)


async def _stub_web_search(**kwargs):
    return ToolResult(
        tool_name="web_search",
        success=True,
        data={
            "status": "success",
            "summary": "Crawled 1 pages, 1 succeeded",
            "pages": [
                {
                    "url": "https://example.com/news",
                    "final_url": "https://example.com/news",
                    "success": True,
                    "title": "News",
                    "excerpt": "Fresh page",
                    "markdown": "fresh page content",
                    "links": [],
                    "metadata": {},
                    "error": None,
                }
            ],
        },
    )


async def _stub_stale_kg_search(**kwargs):
    return ToolResult(
        tool_name="kg_hybrid_search",
        success=True,
        data={
            "status": "success",
            "summary": "Retrieved 1 entity, 0 relationships, and 0 chunks",
            "data": {
                "entities": [
                    {
                        "entity": "Tesla",
                        "last_confirmed_at": "2025-01-01T00:00:00+00:00",
                        "rank": 1,
                    }
                ],
                "relationships": [],
                "chunks": [],
            },
        },
    )


async def _stub_empty_kg_search(**kwargs):
    return ToolResult(
        tool_name="kg_hybrid_search",
        success=True,
        data={
            "status": "success",
            "summary": "Retrieved 0 entities, 0 relationships, and 0 chunks",
            "data": {"entities": [], "relationships": [], "chunks": []},
        },
    )


async def _stub_fresh_kg_search(**kwargs):
    return ToolResult(
        tool_name="kg_hybrid_search",
        success=True,
        data={
            "status": "success",
            "summary": "Retrieved 1 entity, 0 relationships, and 0 chunks",
            "data": {
                "entities": [
                    {
                        "entity": "Tesla",
                        "last_confirmed_at": datetime.now(timezone.utc).isoformat(),
                        "rank": 1,
                    }
                ],
                "relationships": [],
                "chunks": [],
            },
        },
    )


async def _stub_kg_ingest(**kwargs):
    return ToolResult(
        tool_name="kg_ingest",
        success=True,
        data={
            "status": "accepted",
            "summary": "Accepted 1 document(s) for ingestion (track_id=t1)",
        },
        metadata={"track_id": "t1", "document_count": 1},
    )


def _build_registry(*handlers) -> ToolRegistry:
    registry = ToolRegistry()
    for name, handler in handlers:
        registry.register(
            ToolDefinition(
                name=name,
                description=f"{name} test tool",
                input_schema={},
                handler=handler,
            )
        )
    return registry


@pytest.mark.asyncio
async def test_agent_core_auto_ingest_persists_compact_tool_calls_and_injects_recent_calls():
    memory = ConversationMemoryStore()
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=True, enable_kg_ingest=True),
        freshness=FreshnessConfig(enable_auto_ingest=True, threshold_seconds=60),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    route_judge = _CapturingRouteJudge(
        routes=[
            RouteDecision(
                need_tools=True,
                need_memory=False,
                need_web_search=True,
                need_path_explanation=False,
                strategy="freshness_aware_search",
                tool_sequence=[
                    ToolCallPlan(tool="web_search"),
                    ToolCallPlan(tool="kg_hybrid_search"),
                ],
                reason="realtime",
                max_iterations=2,
            ),
            RouteDecision(
                need_tools=False,
                need_memory=False,
                need_web_search=False,
                need_path_explanation=False,
                strategy="simple_answer_no_tool",
                tool_sequence=[],
                reason="preview only",
                max_iterations=1,
            ),
        ]
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=_build_registry(
            ("web_search", _stub_web_search),
            ("kg_hybrid_search", _stub_stale_kg_search),
            ("kg_ingest", _stub_kg_ingest),
        ),
        route_judge=route_judge,
        conversation_memory=memory,
    )

    response = await agent.chat(
        query="latest tesla deliveries",
        session_id="s-auto",
        use_memory=True,
    )

    assert response.metadata["freshness_action"] == "auto_ingested"

    recent_tool_calls = await memory.get_recent_tool_calls("s-auto", assistant_turns=1)
    assert [item["tool"] for item in recent_tool_calls] == [
        "web_search",
        "kg_hybrid_search",
        "kg_ingest",
    ]

    await agent.preview_route(
        query="is it still current?",
        session_id="s-auto",
        use_memory=True,
    )

    assert route_judge.seen_session_contexts[-1]["recent_tool_calls"][0]["tool"] == "web_search"


@pytest.mark.asyncio
async def test_agent_core_correction_refresh_sets_metadata_and_annotation():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=True, enable_kg_ingest=True),
        freshness=FreshnessConfig(enable_auto_ingest=False, threshold_seconds=60),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    route_judge = _CapturingRouteJudge(
        routes=[
            RouteDecision(
                need_tools=True,
                need_memory=False,
                need_web_search=True,
                need_path_explanation=False,
                strategy="correction_and_refresh",
                tool_sequence=[ToolCallPlan(tool="web_search")],
                reason="correction",
                max_iterations=1,
            )
        ]
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=_build_registry(
            ("web_search", _stub_web_search),
            ("kg_hybrid_search", _stub_empty_kg_search),
            ("kg_ingest", _stub_kg_ingest),
        ),
        route_judge=route_judge,
    )

    response = await agent.chat(
        query="this is outdated",
        session_id="s-correct",
        use_memory=True,
    )

    assert response.metadata["freshness_action"] == "user_correction_refresh"
    assert "Refreshed the graph using your correction feedback." in response.answer


@pytest.mark.asyncio
async def test_agent_core_fresh_graph_skips_auto_ingest():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False, enable_kg_ingest=True),
        freshness=FreshnessConfig(enable_auto_ingest=True, threshold_seconds=60),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
    )
    route_judge = _CapturingRouteJudge(
        routes=[
            RouteDecision(
                need_tools=True,
                need_memory=False,
                need_web_search=True,
                need_path_explanation=False,
                strategy="freshness_aware_search",
                tool_sequence=[
                    ToolCallPlan(tool="web_search"),
                    ToolCallPlan(tool="kg_hybrid_search"),
                ],
                reason="realtime",
                max_iterations=2,
            )
        ]
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        tool_registry=_build_registry(
            ("web_search", _stub_web_search),
            ("kg_hybrid_search", _stub_fresh_kg_search),
        ),
        route_judge=route_judge,
    )

    response = await agent.chat(
        query="latest tesla deliveries",
        session_id="s-fresh",
        use_memory=False,
    )

    assert response.metadata["freshness_action"] == "graph_data_fresh"
    assert [item["tool"] for item in response.tool_calls] == [
        "web_search",
        "kg_hybrid_search",
    ]


@pytest.mark.asyncio
async def test_agent_core_executes_generic_tool_input_bindings():
    captured: dict[str, object] = {}

    async def _produce_payload(**kwargs):
        return ToolResult(
            tool_name="payload_producer",
            success=True,
            data={
                "payload": {
                    "documents": ["doc-1", "doc-2"],
                    "source": "custom://payload",
                }
            },
        )

    async def _consume_payload(**kwargs):
        captured["content"] = kwargs.get("content")
        captured["source"] = kwargs.get("source")
        return ToolResult(
            tool_name="payload_consumer",
            success=True,
            data={"status": "accepted"},
        )

    route_judge = _CapturingRouteJudge(
        routes=[
            RouteDecision(
                need_tools=True,
                need_memory=False,
                need_web_search=False,
                need_path_explanation=False,
                strategy="custom_chain",
                tool_sequence=[
                    ToolCallPlan(tool="payload_producer"),
                    ToolCallPlan(
                        tool="payload_consumer",
                        input_bindings={
                            "content": {
                                "from": "previous",
                                "path": "data.payload.documents[]",
                            },
                            "source": {
                                "from": "previous",
                                "path": "data.payload.source",
                            },
                        },
                    ),
                ],
                reason="test",
                max_iterations=2,
            )
        ]
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            freshness=FreshnessConfig(enable_auto_ingest=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        tool_registry=_build_registry(
            ("payload_producer", _produce_payload),
            ("payload_consumer", _consume_payload),
        ),
        route_judge=route_judge,
    )

    response = await agent.chat(
        query="chain the previous output",
        session_id="s-chain-generic",
        use_memory=False,
    )

    assert [item["tool"] for item in response.tool_calls] == [
        "payload_producer",
        "payload_consumer",
    ]
    assert captured["content"] == ["doc-1", "doc-2"]
    assert captured["source"] == "custom://payload"


@pytest.mark.asyncio
async def test_agent_core_can_pipe_web_search_output_into_kg_ingest():
    captured: dict[str, object] = {}

    async def _capturing_kg_ingest(**kwargs):
        captured["content"] = kwargs.get("content")
        captured["source"] = kwargs.get("source")
        return await _stub_kg_ingest(**kwargs)

    route_judge = _CapturingRouteJudge(
        routes=[
            RouteDecision(
                need_tools=True,
                need_memory=False,
                need_web_search=True,
                need_path_explanation=False,
                strategy="kg_ingest_request",
                tool_sequence=[
                    ToolCallPlan(tool="web_search"),
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
                    ),
                ],
                reason="ingest request",
                max_iterations=2,
            )
        ]
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False, enable_kg_ingest=True),
            freshness=FreshnessConfig(enable_auto_ingest=False),
            runtime=AgentRuntimeConfig(default_workspace="", max_iterations=3),
        ),
        tool_registry=_build_registry(
            ("web_search", _stub_web_search),
            ("kg_ingest", _capturing_kg_ingest),
        ),
        route_judge=route_judge,
    )

    response = await agent.chat(
        query="ingest the source into kg",
        session_id="s-chain-web",
        use_memory=False,
    )

    assert [item["tool"] for item in response.tool_calls] == [
        "web_search",
        "kg_ingest",
    ]
    assert captured["content"] == "fresh page content"
    assert captured["source"] == "https://example.com/news"


@pytest.mark.asyncio
async def test_retrieval_freshness_decay_prefers_newer_items():
    older = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()

    class _FakeQueryRAG:
        async def aquery_data(self, query, param):
            return {
                "status": "success",
                "data": {
                    "entities": [
                        {"entity_name": "Legacy", "rank": 1, "last_confirmed_at": older},
                        {"entity_name": "Fresh", "rank": 1, "last_confirmed_at": newer},
                    ],
                    "relationships": [],
                    "chunks": [],
                },
            }

    result = await kg_hybrid_search(
        rag=_FakeQueryRAG(),
        query="latest supplier status",
        freshness_config=FreshnessConfig(
            enable_freshness_decay=True,
            staleness_decay_days=7.0,
        ),
    )

    assert result.success is True
    assert [item["entity_name"] for item in result.data["data"]["entities"]] == [
        "Fresh",
        "Legacy",
    ]


def test_convert_to_user_format_preserves_temporal_metadata_and_rank():
    payload = convert_to_user_format(
        entities_context=[
            {
                "entity": "Tesla",
                "type": "Company",
                "description": "EV company",
                "source_id": "doc-1",
                "file_path": "https://example.com/tesla",
                "created_at": "2026-04-01T00:00:00+00:00",
                "last_confirmed_at": "2026-04-02T00:00:00+00:00",
                "confirmation_count": 3,
                "rank": 8,
            }
        ],
        relations_context=[
            {
                "entity1": "Tesla",
                "entity2": "Battery",
                "description": "uses",
                "keywords": "battery",
                "weight": 1.0,
                "source_id": "doc-2",
                "file_path": "https://example.com/battery",
                "created_at": "2026-04-01T00:00:00+00:00",
                "last_confirmed_at": "2026-04-02T00:00:00+00:00",
                "confirmation_count": 2,
                "rank": 5,
            }
        ],
        chunks=[],
        references=[],
        query_mode="hybrid",
    )

    entity = payload["data"]["entities"][0]
    relationship = payload["data"]["relationships"][0]

    assert entity["created_at"] == "2026-04-01T00:00:00+00:00"
    assert entity["last_confirmed_at"] == "2026-04-02T00:00:00+00:00"
    assert entity["confirmation_count"] == 3
    assert entity["rank"] == 8
    assert relationship["created_at"] == "2026-04-01T00:00:00+00:00"
    assert relationship["last_confirmed_at"] == "2026-04-02T00:00:00+00:00"
    assert relationship["confirmation_count"] == 2
    assert relationship["rank"] == 5


def test_operate_freshness_decay_prefers_newer_items_at_lower_layer():
    older = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    items = [
        {"entity_name": "Legacy", "rank": 2, "last_confirmed_at": older},
        {"entity_name": "Fresh", "rank": 1, "last_confirmed_at": newer},
    ]

    reordered, applied = _sort_items_with_freshness_decay(
        items,
        QueryParam(enable_freshness_decay=True, staleness_decay_days=7.0),
    )

    assert applied is True
    assert [item["entity_name"] for item in reordered] == ["Fresh", "Legacy"]
