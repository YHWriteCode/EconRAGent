from dataclasses import dataclass
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.route_judge import RouteDecision, ToolCallPlan
from kg_agent.api.app import create_app
from kg_agent.config import (
    AgentModelConfig,
    AgentRuntimeConfig,
    KGAgentConfig,
    MCPConfig,
    MCPCapabilityConfig,
    MCPServerConfig,
    ToolConfig,
)
from kg_agent.mcp.adapter import MCPAdapter


class _FakeRAG:
    workspace = ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision
    seen_available_capabilities: list[str] | None = None

    async def plan(self, **kwargs):
        self.seen_available_capabilities = kwargs.get("available_capabilities")
        return self.route


def _build_mcp_config() -> MCPConfig:
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    )
    return MCPConfig(
        servers=[
            MCPServerConfig(
                name="quant-skill",
                command=sys.executable,
                args=[str(fixture_path)],
                startup_timeout_s=5.0,
                tool_timeout_s=5.0,
                discover_tools=True,
            )
        ],
        capabilities=[
            MCPCapabilityConfig(
                name="quant_backtest_skill",
                description="Run a backtest through an external MCP skill.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "symbol": {"type": "string"},
                    },
                    "required": ["query"],
                },
                server="quant-skill",
                remote_name="quant_backtest",
                tags=["quant"],
                planner_exposed=False,
            )
        ],
    )


def test_mcp_config_from_env_parses_servers_and_capabilities(monkeypatch):
    monkeypatch.setenv(
        "KG_AGENT_MCP_SERVERS_JSON",
        '[{"name":"quant-skill","command":"python","args":["server.py"],"tool_timeout_s":30,"discover_tools":true}]',
    )
    monkeypatch.setenv(
        "KG_AGENT_MCP_CAPABILITIES_JSON",
        (
            '[{"name":"quant_backtest_skill","description":"Run a backtest.",'
            '"server":"quant-skill","remote_name":"quant_backtest","input_schema":{"type":"object"},'
            '"planner_exposed":false,"tags":["quant"]}]'
        ),
    )

    config = KGAgentConfig.from_env()

    assert len(config.mcp.servers) == 1
    assert config.mcp.servers[0].name == "quant-skill"
    assert config.mcp.servers[0].discover_tools is True
    assert len(config.mcp.capabilities) == 1
    assert config.mcp.capabilities[0].name == "quant_backtest_skill"
    assert config.mcp.capabilities[0].planner_exposed is False


@pytest.mark.asyncio
async def test_agent_core_hides_external_mcp_capabilities_from_auto_routing_by_default():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=_build_mcp_config(),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    route_judge = _StubRouteJudge(
        route=RouteDecision(
            need_tools=False,
            need_memory=False,
            need_web_search=False,
            need_path_explanation=False,
            strategy="simple_answer_no_tool",
            tool_sequence=[],
            reason="test",
            max_iterations=1,
        )
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=route_judge,
        mcp_adapter=adapter,
    )

    await agent.preview_route(
        query="hello",
        session_id="mcp-preview",
        use_memory=False,
    )

    assert "quant_backtest_skill" not in (route_judge.seen_available_capabilities or [])
    assert "portfolio_stats" not in (route_judge.seen_available_capabilities or [])
    await adapter.close()


@pytest.mark.asyncio
async def test_mcp_adapter_discovers_tools_and_skips_static_remote_duplicates():
    adapter = MCPAdapter(_build_mcp_config())

    discovered = await adapter.discover_capabilities(reserved_names={"echo"})
    listed = adapter.list_capability_configs()

    try:
        assert len(discovered) == 1
        assert discovered[0].name == "portfolio_stats"
        assert discovered[0].remote_name == "portfolio_stats"
        assert discovered[0].planner_exposed is False
        assert any(item.name == "quant_backtest_skill" for item in listed)
        assert any(item.name == "portfolio_stats" for item in listed)
        assert not any(item.name == "quant_backtest" for item in listed)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_agent_core_registers_discovered_capabilities_for_explicit_invocation():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=_build_mcp_config(),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
    )

    try:
        await agent.initialize_external_capabilities()
        listed = agent.list_tools()
        response = await agent.invoke_capability(
            capability_name="portfolio_stats",
            session_id="mcp-portfolio",
            query="summarize portfolio p1",
            args={"portfolio_id": "p1"},
        )

        assert any(item["name"] == "portfolio_stats" for item in listed)
        tool_call = response.result
        assert tool_call["tool"] == "portfolio_stats"
        assert tool_call["success"] is True
        assert tool_call["metadata"]["executor"] == "mcp"
        assert tool_call["data"]["structured_content"]["tool"] == "portfolio_stats"
        assert tool_call["data"]["structured_content"]["echo"]["portfolio_id"] == "p1"
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_agent_core_executes_external_mcp_capability_and_lists_it():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=_build_mcp_config(),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    route = RouteDecision(
        need_tools=True,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="external_skill_request",
        tool_sequence=[
            ToolCallPlan(
                tool="quant_backtest_skill",
                args={"symbol": "AAPL"},
            )
        ],
        reason="manual external invocation",
        max_iterations=1,
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        mcp_adapter=adapter,
    )

    listed = agent.list_tools()
    response = await agent.chat(
        query="run a backtest for AAPL",
        session_id="mcp-chat",
        use_memory=False,
        debug=True,
    )

    assert any(item["name"] == "quant_backtest_skill" for item in listed)
    external = next(item for item in listed if item["name"] == "quant_backtest_skill")
    assert external["kind"] == "external_mcp"
    assert external["executor"] == "mcp"

    tool_call = response.tool_calls[0]
    assert tool_call["tool"] == "quant_backtest_skill"
    assert tool_call["success"] is True
    assert tool_call["metadata"]["executor"] == "mcp"
    assert tool_call["data"]["structured_content"]["echo"]["query"] == "run a backtest for AAPL"
    assert tool_call["data"]["structured_content"]["echo"]["symbol"] == "AAPL"
    assert "rag" not in tool_call["data"]["structured_content"]["echo"]
    assert "memory_store" not in tool_call["data"]["structured_content"]["echo"]

    await adapter.close()


def test_capability_invoke_endpoint_executes_external_mcp_capability():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=_build_mcp_config(),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
    )
    app = create_app(agent_core=agent, config=config)

    with TestClient(app) as client:
        response = client.post(
            "/agent/capabilities/quant_backtest_skill/invoke",
            json={
                "session_id": "mcp-invoke",
                "query": "run a backtest for AAPL",
                "args": {"symbol": "AAPL"},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability"]["name"] == "quant_backtest_skill"
    assert payload["capability"]["kind"] == "external_mcp"
    assert payload["result"]["tool"] == "quant_backtest_skill"
    assert payload["result"]["success"] is True
    assert payload["result"]["data"]["structured_content"]["echo"]["query"] == (
        "run a backtest for AAPL"
    )
    assert payload["result"]["data"]["structured_content"]["echo"]["symbol"] == "AAPL"
    assert payload["metadata"]["executor"] == "mcp"


def test_tools_endpoint_lists_discovered_mcp_capabilities():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=_build_mcp_config(),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
    )
    app = create_app(agent_core=agent, config=config)

    with TestClient(app) as client:
        response = client.get("/agent/tools")

    assert response.status_code == 200
    payload = response.json()
    tool_names = [item["name"] for item in payload["tools"]]
    assert "quant_backtest_skill" in tool_names
    assert "portfolio_stats" in tool_names
