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
from kg_agent.crawler.crawler_adapter import CrawledPage
from kg_agent.tools.base import ToolDefinition
from kg_agent.tools.kg_ingest import kg_ingest
from kg_agent.tools.web_search import web_search


class _CapturingRAG:
    workspace = "web-ingest-chain-test"

    def __init__(self):
        self.insert_calls: list[dict] = []

    async def ainsert(self, input, file_paths=None):
        self.insert_calls.append({"input": input, "file_paths": file_paths})
        return f"track-{len(self.insert_calls)}"


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


class _CrawlerAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    async def crawl_urls(
        self,
        urls: list[str],
        *,
        max_pages: int | None = None,
        max_content_chars: int | None = None,
    ):
        self.calls.append(
            {
                "urls": list(urls),
                "max_pages": max_pages,
                "max_content_chars": max_content_chars,
            }
        )
        return [
            CrawledPage(
                url="https://example.com/policy",
                final_url="https://example.com/policy",
                success=True,
                title="Policy page",
                markdown="Policy support helped BYD expand battery production.",
                excerpt="Policy support helped BYD expand battery production.",
            )
        ]

    async def discover_urls(self, query: str, *, top_k: int = 5):
        raise AssertionError("This chain should use direct URLs, not query discovery")


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="web_search",
            description="Web search tool",
            input_schema={},
            handler=web_search,
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_ingest",
            description="KG ingest tool",
            input_schema={},
            handler=kg_ingest,
        )
    )
    return registry


@pytest.mark.asyncio
async def test_web_search_to_kg_ingest_chain_via_agent_core():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(
            enable_memory=False,
            enable_quant=False,
            enable_web_search=True,
            enable_kg_ingest=True,
        ),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=2),
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=True,
        need_path_explanation=False,
        strategy="kg_ingest_request",
        tool_sequence=[
            ToolCallPlan(
                tool="web_search",
                args={"urls": ["https://example.com/policy"]},
            ),
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
        reason="Ingest crawled web content into KG.",
        max_iterations=2,
    )
    rag = _CapturingRAG()
    crawler = _CrawlerAdapter()
    agent = AgentCore(
        rag=rag,
        config=config,
        tool_registry=_build_registry(),
        route_judge=_StubRouteJudge(route=route),
        crawler_adapter=crawler,
    )

    response = await agent.chat(
        query="请抓取并写入知识图谱 https://example.com/policy",
        session_id="web-ingest-session",
        use_memory=False,
        debug=True,
    )

    assert [item["tool"] for item in response.tool_calls] == [
        "web_search",
        "kg_ingest",
    ]
    assert response.tool_calls[0]["success"] is True
    assert response.tool_calls[1]["success"] is True
    assert crawler.calls == [
        {
            "urls": ["https://example.com/policy"],
            "max_pages": 1,
            "max_content_chars": None,
        }
    ]
    assert rag.insert_calls == [
        {
            "input": "Policy support helped BYD expand battery production.",
            "file_paths": "https://example.com/policy",
        }
    ]
