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
from kg_agent.skills.runtime_client import MCPBasedSkillRuntimeClient
from kg_agent.tools.base import ToolResult


class _FakeRAG:
    workspace = ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


class _FallbackArtifactsAdapter:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root.resolve()
        self.server_config = MCPServerConfig(
            name="skill-runtime",
            command="docker",
            args=[
                "run",
                "--rm",
                "-i",
                "-v",
                f"{self.runs_root.as_posix()}:/workspace/runs",
                "fake-image",
                "python",
                "/app/server.py",
            ],
        )

    async def invoke_remote_tool(self, *, server_name: str, remote_name: str, arguments: dict):
        if remote_name == "get_run_status":
            return ToolResult(
                tool_name=remote_name,
                success=True,
                data={
                    "structured_content": {
                        "summary": "loaded run status",
                        "run_id": arguments["run_id"],
                        "skill_name": "financial-researching",
                        "run_status": "completed",
                        "status": "completed",
                        "success": True,
                        "shell_mode": "free_shell",
                        "runtime_target": {
                            "platform": "linux",
                            "shell": "/bin/sh",
                            "workspace_root": "/workspace",
                            "workdir": "/workspace",
                            "network_allowed": True,
                            "supports_python": True,
                        },
                        "workspace": "/workspace/runs/run-42",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "finished_at": "2026-01-01T00:05:00+00:00",
                        "failure_reason": None,
                        "runtime": {"delivery": "durable_worker", "queue_state": "completed"},
                        "preflight": {"ok": True, "status": "ok", "failure_reason": None},
                        "repair_attempted": False,
                        "repair_succeeded": False,
                        "repaired_from_run_id": None,
                        "repair_attempt_count": 0,
                        "repair_attempt_limit": 3,
                        "repair_history": [],
                        "bootstrap_attempted": True,
                        "bootstrap_succeeded": True,
                        "bootstrap_attempt_count": 1,
                        "bootstrap_attempt_limit": 2,
                        "bootstrap_history": [],
                        "cancel_requested": False,
                    }
                },
            )
        if remote_name == "get_run_artifacts":
            return ToolResult(
                tool_name=remote_name,
                success=False,
                error="Separator is not found, and chunk exceed the limit",
            )
        raise AssertionError(f"Unexpected remote tool: {remote_name}")

    def get_server_config(self, server_name: str):
        return self.server_config


class _DockerWorkspaceAdapter:
    def __init__(self, runs_root: Path):
        self.runs_root = runs_root.resolve()
        self.server_config = MCPServerConfig(
            name="skill-runtime",
            command="docker",
            args=[
                "run",
                "--rm",
                "-i",
                "-v",
                f"{self.runs_root.as_posix()}:/workspace/runs",
                "fake-image",
                "python",
                "/app/server.py",
            ],
        )

    async def invoke_remote_tool(self, *, server_name: str, remote_name: str, arguments: dict):
        if remote_name != "run_skill_task":
            raise AssertionError(f"Unexpected remote tool: {remote_name}")
        return ToolResult(
            tool_name=remote_name,
            success=True,
            data={
                "structured_content": {
                    "summary": "run enqueued",
                    "run_id": "skill-run-host-path",
                    "skill_name": arguments["skill_name"],
                    "run_status": "running",
                    "status": "running",
                    "success": True,
                    "shell_mode": "conservative",
                    "runtime_target": {
                        "platform": "linux",
                        "shell": "/bin/sh",
                        "workspace_root": "/workspace",
                        "workdir": "/workspace",
                        "network_allowed": False,
                        "supports_python": True,
                    },
                    "workspace": "/workspace/runs/run-42",
                    "runtime": {"delivery": "durable_worker", "queue_state": "queued"},
                    "command_plan": arguments["command_plan"],
                    "logs_preview": {"stdout": "", "stderr": ""},
                    "artifacts": [],
                }
            },
        )

    def get_server_config(self, server_name: str):
        return self.server_config


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
    assert response.tool_calls[0]["data"]["run_status"] == "completed"
    assert response.tool_calls[0]["data"]["status"] == "completed"
    assert response.tool_calls[0]["data"]["shell_mode"] == "conservative"
    assert response.tool_calls[0]["data"]["runtime_target"]["platform"] == "linux"
    assert response.tool_calls[0]["data"]["preflight"]["ok"] is True
    assert response.tool_calls[0]["data"]["repair_attempted"] is False
    assert response.tool_calls[0]["data"]["runtime"]["executor"] == "mcp"
    assert response.tool_calls[0]["data"]["runtime"]["server"] == "skill-runtime"
    assert response.tool_calls[0]["data"]["runtime"]["status_tool_name"] == "get_run_status"
    assert response.tool_calls[0]["data"]["runtime"]["cancel_tool_name"] == "cancel_skill_run"
    assert response.tool_calls[0]["data"]["runtime"]["logs_tool_name"] == "get_run_logs"
    assert response.tool_calls[0]["data"]["runtime"]["artifacts_tool_name"] == (
        "get_run_artifacts"
    )
    assert response.tool_calls[0]["data"]["run_id"] == "skill-run-123"
    assert response.tool_calls[0]["data"]["command"] == (
        "python scripts/run_report.py --topic 'example' --notes 'runtime test'"
    )
    assert response.tool_calls[0]["data"]["command_plan"]["mode"] == "explicit"
    assert response.tool_calls[0]["data"]["artifacts"] == [
        {"path": "report.md", "size_bytes": 128}
    ]
    assert response.tool_calls[0]["data"]["logs_preview"]["stdout"] == "report generated"
    assert response.tool_calls[0]["data"]["runtime_result"]["echo"]["skill_name"] == (
        "example-skill"
    )
    assert (
        response.tool_calls[0]["data"]["runtime_result"]["echo"]["constraints"]["shell_command"]
        == "python scripts/run_report.py --topic 'example' --notes 'runtime test'"
    )


@pytest.mark.asyncio
async def test_agent_core_can_fetch_skill_run_status_via_mcp_runtime_transport():
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
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
    )

    try:
        status = await agent.get_skill_run_status(run_id="skill-run-123")
    finally:
        await adapter.close()

    assert status["run_id"] == "skill-run-123"
    assert status["run_status"] == "completed"
    assert status["status"] == "completed"
    assert status["shell_mode"] == "conservative"
    assert status["runtime_target"]["platform"] == "linux"
    assert status["runtime"]["delivery"] == "durable_worker"
    assert status["runtime"]["log_streaming"] is False
    assert status["runtime"]["log_transport"] == "poll"
    assert status["preflight"]["ok"] is True
    assert status["command_plan"]["mode"] == "explicit"


@pytest.mark.asyncio
async def test_agent_core_can_cancel_skill_run_via_mcp_runtime_transport():
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
    adapter = MCPAdapter(config.mcp)
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
    )

    try:
        status = await agent.cancel_skill_run(run_id="skill-run-123")
    finally:
        await adapter.close()

    assert status["run_id"] == "skill-run-123"
    assert status["run_status"] == "failed"
    assert status["status"] == "failed"
    assert status["failure_reason"] == "cancelled"
    assert status["cancel_requested"] is True
    assert status["runtime"]["delivery"] == "durable_worker"
    assert status["runtime"]["log_transport"] == "poll"


@pytest.mark.asyncio
async def test_runtime_client_falls_back_to_host_workspace_for_artifacts_on_transport_overflow(
    tmp_path: Path,
):
    runs_root = (tmp_path / "mounted-workspace" / "runs").resolve()
    run_workspace = runs_root / "run-42"
    output_dir = run_workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text('{"ok":true}', encoding="utf-8")
    (run_workspace / ".skill_bootstrap" / "site-packages").mkdir(parents=True, exist_ok=True)
    (run_workspace / ".skill_bootstrap" / "site-packages" / "ignored.py").write_text(
        "ignored",
        encoding="utf-8",
    )

    client = MCPBasedSkillRuntimeClient(
        adapter=_FallbackArtifactsAdapter(runs_root),
        config=SkillRuntimeConfig(server="skill-runtime"),
    )

    artifacts = await client.get_run_artifacts(run_id="skill-run-overflow")

    assert artifacts["run_status"] == "completed"
    assert artifacts["workspace"] == str(run_workspace)
    assert artifacts["runtime"]["artifacts_host_fallback"] is True
    assert artifacts["runtime"]["container_workspace"] == "/workspace/runs/run-42"
    assert artifacts["artifacts"] == [{"path": "output/report.json", "size_bytes": 11}]


@pytest.mark.asyncio
async def test_runtime_client_maps_docker_workspace_to_host_path_in_run_payload(
    tmp_path: Path,
):
    runs_root = (tmp_path / "mounted-workspace" / "runs").resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    client = MCPBasedSkillRuntimeClient(
        adapter=_DockerWorkspaceAdapter(runs_root),
        config=SkillRuntimeConfig(server="skill-runtime"),
    )

    request = type(
        "_Req",
        (),
        {
            "skill_name": "example-skill",
            "goal": "Run example skill",
            "user_query": "run example skill",
            "workspace": None,
            "constraints": {},
        },
    )()
    loaded_skill = type(
        "_Loaded",
        (),
        {"skill": type("_Skill", (), {"path": Path("skills/example-skill").resolve()})()},
    )()
    command_plan = type(
        "_Plan",
        (),
        {
            "to_dict": lambda self: {
                "skill_name": "example-skill",
                "goal": "Run example skill",
                "user_query": "run example skill",
                "runtime_target": {
                    "platform": "linux",
                    "shell": "/bin/sh",
                    "workspace_root": "/workspace",
                    "workdir": "/workspace",
                    "network_allowed": False,
                    "supports_python": True,
                },
                "constraints": {},
                "command": "python scripts/run_report.py",
                "mode": "explicit",
                "shell_mode": "conservative",
                "generated_files": [],
                "missing_fields": [],
                "failure_reason": None,
                "hints": {},
            }
        },
    )()

    run_record = await client.run_skill_task(
        request=request,
        loaded_skill=loaded_skill,
        command_plan=command_plan,
    )

    assert run_record.workspace == str((runs_root / "run-42").resolve())
    assert run_record.runtime["container_workspace"] == "/workspace/runs/run-42"
