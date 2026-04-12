from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.route_judge import RouteDecision
from kg_agent.config import (
    AgentModelConfig,
    AgentRuntimeConfig,
    KGAgentConfig,
    MCPConfig,
    MCPServerConfig,
    SkillRuntimeConfig,
    ToolConfig,
)
from kg_agent.mcp.adapter import MCPAdapter
from kg_agent.skills import SkillPlan


class _FakeRAG:
    workspace = ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


@pytest.mark.asyncio
async def test_agent_core_can_execute_skill_plan_via_mcp_runtime_transport():
    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "fake_skill_runtime_server.py"
    )
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=MCPConfig(
            servers=[
                MCPServerConfig(
                    name="skill-runtime",
                    command=sys.executable,
                    args=[str(fixture_path)],
                    startup_timeout_s=5.0,
                    tool_timeout_s=5.0,
                    discover_tools=False,
                )
            ],
            capabilities=[],
        ),
        skill_runtime=SkillRuntimeConfig(server="skill-runtime"),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="runtime skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="example-skill",
            goal="Use example skill",
            reason="Matched local skill",
            constraints={
                "shell_command": "python scripts/run_report.py --topic 'example' --notes 'runtime test'"
            },
        ),
    )
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        mcp_adapter=adapter,
    )

    try:
        response = await agent.chat(
            query="run example skill",
            session_id="skill-runtime-session",
            use_memory=False,
            debug=True,
        )
    finally:
        await adapter.close()

    assert response.tool_calls[0]["tool"] == "skill:example-skill"
    assert response.tool_calls[0]["metadata"]["executor"] == "skill"
    assert response.tool_calls[0]["data"]["execution_mode"] == "shell"
    assert response.tool_calls[0]["data"]["runtime"]["executor"] == "mcp"
    assert response.tool_calls[0]["data"]["runtime"]["server"] == "skill-runtime"
    assert response.tool_calls[0]["data"]["runtime"]["logs_tool_name"] == "get_run_logs"
    assert response.tool_calls[0]["data"]["runtime"]["artifacts_tool_name"] == (
        "get_run_artifacts"
    )
    assert response.tool_calls[0]["data"]["run_id"] == "skill-run-123"
    assert response.tool_calls[0]["data"]["command"] == (
        "python scripts/run_report.py --topic 'example' --notes 'runtime test'"
    )
    assert response.tool_calls[0]["data"]["artifacts"][0]["path"] == "report.md"
    assert response.tool_calls[0]["data"]["logs_preview"]["stdout"] == "report generated"
    assert response.tool_calls[0]["data"]["runtime_result"]["echo"]["skill_name"] == (
        "example-skill"
    )
    assert (
        response.tool_calls[0]["data"]["runtime_result"]["echo"]["constraints"]["shell_command"]
        == "python scripts/run_report.py --topic 'example' --notes 'runtime test'"
    )
