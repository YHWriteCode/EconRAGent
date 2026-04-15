from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from kg_agent.agent.agent_core import AgentCore
from kg_agent.api.app import create_app
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
from kg_agent.skills import SkillLoader, SkillRegistry


class _FakeRAG:
    workspace = ""


def _build_agent_for_skill_api(tmp_path: Path) -> AgentCore:
    skill_dir = tmp_path / "skills" / "xlsx-automation"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# xlsx-automation\n\nPrepare and validate spreadsheet workflows.\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "usage.md").write_text(
        "Reference instructions for spreadsheet runs.",
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "run.ps1").write_text(
        "Write-Output 'spreadsheet skill'",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path / "skills")
    loader = SkillLoader(registry)
    return AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(
            agent_model=AgentModelConfig(provider="disabled"),
            tool_config=ToolConfig(enable_memory=False),
            runtime=AgentRuntimeConfig(
                default_workspace="",
                max_iterations=1,
                skills_dir=str(tmp_path / "skills"),
            ),
        ),
        skill_registry=registry,
        skill_loader=loader,
    )


def _build_runtime_agent_for_skill_api(tmp_path: Path) -> AgentCore:
    agent = _build_agent_for_skill_api(tmp_path)
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "fake_skill_runtime_server.py"
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
        runtime=AgentRuntimeConfig(
            default_workspace="",
            max_iterations=1,
            skills_dir=str(tmp_path / "skills"),
        ),
    )
    return AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=MCPAdapter(config.mcp),
        skill_registry=agent.skill_registry,
        skill_loader=agent.skill_loader,
    )


def test_skill_endpoints_list_read_file_and_invoke(tmp_path: Path):
    app = create_app(agent_core=_build_agent_for_skill_api(tmp_path))

    with TestClient(app) as client:
        skills_response = client.get("/agent/skills")
        detail_response = client.get("/agent/skills/xlsx-automation")
        file_response = client.get(
            "/agent/skills/xlsx-automation/files/references/usage.md"
        )
        invoke_response = client.post(
            "/agent/skills/xlsx-automation/invoke",
            json={
                "session_id": "skill-api-session",
                "goal": "Prepare a spreadsheet automation run",
                "query": "use the spreadsheet skill for workbook A",
                "workspace": "finance",
                "constraints": {"output": "report"},
            },
        )

    assert skills_response.status_code == 200
    skills_payload = skills_response.json()
    assert [item["name"] for item in skills_payload["skills"]] == ["xlsx-automation"]

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["skill"]["description"] == (
        "Prepare and validate spreadsheet workflows."
    )
    inventory_paths = [item["path"] for item in detail_payload["file_inventory"]]
    assert "SKILL.md" in inventory_paths
    assert "references/usage.md" in inventory_paths
    assert "scripts/run.ps1" in inventory_paths

    assert file_response.status_code == 200
    file_payload = file_response.json()
    assert file_payload["kind"] == "reference"
    assert "Reference instructions" in file_payload["content"]

    assert invoke_response.status_code == 200
    invoke_payload = invoke_response.json()
    assert invoke_payload["skill"]["name"] == "xlsx-automation"
    assert invoke_payload["result"]["tool"] == "skill:xlsx-automation"
    assert invoke_payload["result"]["success"] is True
    assert invoke_payload["result"]["data"]["run_status"] == "planned"
    assert invoke_payload["result"]["data"]["status"] == "planned"
    assert invoke_payload["result"]["data"]["command_plan"]["mode"] == "inferred"
    assert invoke_payload["result"]["data"]["runtime_target"]["platform"] == "linux"
    assert invoke_payload["result"]["data"]["workspace"] == "finance"
    assert invoke_payload["metadata"]["kind"] == "skill"
    assert invoke_payload["metadata"]["executor"] == "skill"


def test_skill_read_rejects_path_escape(tmp_path: Path):
    agent = _build_agent_for_skill_api(tmp_path)

    with pytest.raises(ValueError, match="relative_path must stay inside the skill directory"):
        agent.read_skill_file("xlsx-automation", "../outside.txt")


def test_skill_run_endpoints_return_logs_and_artifacts(tmp_path: Path):
    app = create_app(agent_core=_build_runtime_agent_for_skill_api(tmp_path))

    with TestClient(app) as client:
        invoke_response = client.post(
            "/agent/skills/xlsx-automation/invoke",
            json={
                "session_id": "skill-run-api-session",
                "goal": "Run spreadsheet shell workflow",
                "query": "execute the runtime skill",
                "constraints": {
                    "shell_command": "python scripts/run_report.py --topic 'example'"
                },
            },
        )
        run_id = invoke_response.json()["result"]["data"]["run_id"]
        status_response = client.get(f"/agent/skill-runs/{run_id}")
        logs_response = client.get(f"/agent/skill-runs/{run_id}/logs")
        artifacts_response = client.get(f"/agent/skill-runs/{run_id}/artifacts")

    assert invoke_response.status_code == 200
    invoke_payload = invoke_response.json()
    assert invoke_payload["result"]["data"]["execution_mode"] == "shell"
    assert invoke_payload["result"]["data"]["run_id"] == "skill-run-123"
    assert invoke_payload["result"]["data"]["run_status"] == "running"
    assert invoke_payload["result"]["data"]["status"] == "running"
    assert invoke_payload["result"]["data"]["command_plan"]["mode"] == "explicit"
    assert invoke_payload["result"]["data"]["shell_mode"] == "conservative"
    assert invoke_payload["result"]["data"]["runtime_target"]["platform"] == "linux"
    assert invoke_payload["result"]["data"]["preflight"]["ok"] is True

    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["run_id"] == "skill-run-123"
    assert status_payload["run_status"] == "completed"
    assert status_payload["status"] == "completed"
    assert status_payload["shell_mode"] == "conservative"
    assert status_payload["runtime_target"]["platform"] == "linux"
    assert status_payload["runtime"]["delivery"] == "durable_worker"
    assert status_payload["preflight"]["ok"] is True
    assert status_payload["command_plan"]["mode"] == "explicit"
    assert status_payload["cancel_requested"] is False

    assert logs_response.status_code == 200
    logs_payload = logs_response.json()
    assert logs_payload["run_id"] == "skill-run-123"
    assert logs_payload["run_status"] == "completed"
    assert logs_payload["shell_mode"] == "conservative"
    assert logs_payload["runtime_target"]["platform"] == "linux"
    assert logs_payload["runtime"]["delivery"] == "durable_worker"
    assert logs_payload["preflight"]["ok"] is True
    assert logs_payload["stdout"] == "report generated"
    assert logs_payload["success"] is True
    assert logs_payload["cancel_requested"] is False

    assert artifacts_response.status_code == 200
    artifacts_payload = artifacts_response.json()
    assert artifacts_payload["run_id"] == "skill-run-123"
    assert artifacts_payload["run_status"] == "completed"
    assert artifacts_payload["shell_mode"] == "conservative"
    assert artifacts_payload["runtime_target"]["platform"] == "linux"
    assert artifacts_payload["runtime"]["delivery"] == "durable_worker"
    assert artifacts_payload["preflight"]["ok"] is True
    assert artifacts_payload["artifacts"][0]["path"] == "report.md"
    assert artifacts_payload["workspace"] == "/workspace/skill-run-123"
    assert artifacts_payload["cancel_requested"] is False


def test_skill_run_cancel_endpoint_returns_cancelled_status(tmp_path: Path):
    app = create_app(agent_core=_build_runtime_agent_for_skill_api(tmp_path))

    with TestClient(app) as client:
        invoke_response = client.post(
            "/agent/skills/xlsx-automation/invoke",
            json={
                "session_id": "skill-run-api-session",
                "goal": "Run spreadsheet shell workflow",
                "query": "execute the runtime skill",
                "constraints": {
                    "shell_command": "python scripts/run_report.py --topic 'example'"
                },
            },
        )
        run_id = invoke_response.json()["result"]["data"]["run_id"]
        cancel_response = client.post(f"/agent/skill-runs/{run_id}/cancel")

    assert invoke_response.status_code == 200
    assert cancel_response.status_code == 200
    cancel_payload = cancel_response.json()
    assert cancel_payload["run_id"] == "skill-run-123"
    assert cancel_payload["run_status"] == "failed"
    assert cancel_payload["status"] == "failed"
    assert cancel_payload["failure_reason"] == "cancelled"
    assert cancel_payload["cancel_requested"] is True
    assert cancel_payload["runtime"]["delivery"] == "durable_worker"


def test_skill_run_logs_endpoint_returns_404_for_unknown_run(tmp_path: Path):
    app = create_app(agent_core=_build_runtime_agent_for_skill_api(tmp_path))

    with TestClient(app) as client:
        status_response = client.get("/agent/skill-runs/missing-run")
        response = client.get("/agent/skill-runs/missing-run/logs")

    assert status_response.status_code == 404
    status_payload = status_response.json()
    assert status_payload["error"]["code"] == "not_found"
    assert "Unknown run_id: missing-run" in status_payload["error"]["message"]
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "not_found"
    assert "Unknown run_id: missing-run" in payload["error"]["message"]
