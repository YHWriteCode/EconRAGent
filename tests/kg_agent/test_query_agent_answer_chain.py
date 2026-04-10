from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
from kg_agent.crawler.crawl_state_store import CrawlStateRecord, JsonCrawlStateStore
from kg_agent.tools.base import ToolDefinition
from kg_agent.tools.retrieval_tools import kg_hybrid_search


class _CapturingRAG:
    workspace = "query-answer-chain-test"

    def __init__(self):
        self.query_calls: list[dict] = []

    async def aquery_data(self, query: str, param=None):
        self.query_calls.append({"query": query, "param": param})
        return {
            "status": "success",
            "data": {
                "entities": [
                    {
                        "entity": "BYD",
                        "rank": 1.0,
                    }
                ],
                "relationships": [],
                "chunks": [
                    {
                        "content": "Policy support helped BYD expand battery production.",
                        "file_path": "https://example.com/policy",
                    }
                ],
            },
        }


class _ChunkLookupStore:
    def __init__(self, payload_by_chunk_id: dict[str, dict]):
        self.payload_by_chunk_id = dict(payload_by_chunk_id)

    async def get_by_ids(self, ids: list[str]):
        results = []
        for chunk_id in ids:
            payload = self.payload_by_chunk_id.get(chunk_id)
            if payload is None:
                results.append(None)
                continue
            results.append({"_id": chunk_id, **payload})
        return results


class _LifecycleAwareRAG:
    workspace = "query-answer-lifecycle-test"

    def __init__(self):
        self.query_calls: list[dict] = []
        self.text_chunks = _ChunkLookupStore(
            {
                "chunk-active": {
                    "full_doc_id": "doc-active",
                    "file_path": "https://example.com/news",
                },
                "chunk-expired": {
                    "full_doc_id": "doc-expired",
                    "file_path": "https://example.com/news",
                },
            }
        )

    async def aquery_data(self, query: str, param=None):
        self.query_calls.append({"query": query, "param": param})
        return {
            "status": "success",
            "data": {
                "entities": [
                    {
                        "entity_name": "FreshEV",
                        "source_id": "chunk-active",
                        "file_path": "https://example.com/news",
                    },
                    {
                        "entity_name": "LegacyEV",
                        "source_id": "chunk-expired",
                        "file_path": "https://example.com/news",
                    },
                ],
                "relationships": [
                    {
                        "src_id": "FreshEV",
                        "tgt_id": "Battery",
                        "source_id": "chunk-active<SEP>chunk-expired",
                        "file_path": "https://example.com/news",
                    }
                ],
                "chunks": [
                    {
                        "content": "Fresh version of the news item.",
                        "file_path": "https://example.com/news",
                        "chunk_id": "chunk-active",
                    },
                    {
                        "content": "Expired version of the news item.",
                        "file_path": "https://example.com/news",
                        "chunk_id": "chunk-expired",
                    },
                ],
                "references": [],
            },
            "metadata": {},
        }


class _CapturingLLM:
    def __init__(self):
        self.calls: list[dict] = []

    def is_available(self):
        return True

    async def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        return "根据知识库，政策支持帮助比亚迪扩产。"

    async def stream_text(self, **kwargs):
        if False:
            yield ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="kg_hybrid_search",
            description="Hybrid retrieval tool",
            input_schema={},
            handler=kg_hybrid_search,
        )
    )
    return registry


@pytest.mark.asyncio
async def test_query_to_agent_to_kgdb_to_llm_answer_chain():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="factual_qa",
        tool_sequence=[ToolCallPlan(tool="kg_hybrid_search")],
        reason="Use hybrid retrieval for factual QA.",
        max_iterations=1,
    )
    rag = _CapturingRAG()
    llm = _CapturingLLM()
    agent = AgentCore(
        rag=rag,
        config=config,
        tool_registry=_build_registry(),
        route_judge=_StubRouteJudge(route=route),
    )
    agent.llm_client = llm

    response = await agent.chat(
        query="政策如何影响比亚迪？",
        session_id="query-answer-session",
        use_memory=False,
        debug=True,
    )

    assert len(rag.query_calls) == 1
    assert rag.query_calls[0]["query"] == "政策如何影响比亚迪？"
    assert response.tool_calls[0]["tool"] == "kg_hybrid_search"
    assert response.tool_calls[0]["success"] is True
    assert response.answer == "根据知识库，政策支持帮助比亚迪扩产。"
    assert len(llm.calls) == 1
    assert "Policy support helped BYD expand battery production." in llm.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_query_chain_filters_expired_short_term_crawler_content(tmp_path):
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="factual_qa",
        tool_sequence=[ToolCallPlan(tool="kg_hybrid_search")],
        reason="Use hybrid retrieval for factual QA.",
        max_iterations=1,
    )
    crawl_state_store = JsonCrawlStateStore(str(tmp_path / "scheduler-state.json"))
    await crawl_state_store.put_record(
        CrawlStateRecord(
            source_id="news-source",
            item_active_doc_ids={"https://example.com/news": "doc-active"},
            doc_expires_at={
                "doc-expired": (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat()
            },
        )
    )
    rag = _LifecycleAwareRAG()
    llm = _CapturingLLM()
    agent = AgentCore(
        rag=rag,
        config=config,
        tool_registry=_build_registry(),
        route_judge=_StubRouteJudge(route=route),
        crawl_state_store=crawl_state_store,
    )
    agent.llm_client = llm

    response = await agent.chat(
        query="最新电池新闻是什么？",
        session_id="query-answer-lifecycle-session",
        use_memory=False,
        debug=True,
    )

    tool_payload = response.tool_calls[0]["data"]
    assert response.tool_calls[0]["success"] is True
    assert [item["entity_name"] for item in tool_payload["data"]["entities"]] == ["FreshEV"]
    assert [item["content"] for item in tool_payload["data"]["chunks"]] == [
        "Fresh version of the news item."
    ]
    assert tool_payload["data"]["relationships"][0]["source_id"] == "chunk-active"
    assert tool_payload["metadata"]["crawler_lifecycle_filter"] == {
        "applied": True,
        "filtered_chunk_count": 1,
        "filtered_entity_count": 1,
        "filtered_relationship_count": 0,
        "active_short_term_doc_count": 1,
        "expired_short_term_doc_count": 1,
    }
    assert "Fresh version of the news item." in llm.calls[0]["user_prompt"]
    assert "Expired version of the news item." not in llm.calls[0]["user_prompt"]
