from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

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
from kg_agent.skills import (
    MCPBasedSkillRuntimeClient,
    SkillCommandPlan,
    SkillExecutor,
    SkillGeneratedFile,
    SkillLoader,
    SkillPlan,
    SkillRegistry,
    SkillRuntimeTarget,
)


DOCKER_SKILL_RUNTIME_IMAGE = "lightrag-mcp-skill-service:latest"


class _FakeRAG:
    workspace = ""


@dataclass
class _StubRouteJudge:
    route: RouteDecision

    async def plan(self, **kwargs):
        return self.route


class _StaticCommandPlanner:
    def __init__(self, command_plan: SkillCommandPlan):
        self.command_plan = command_plan
        self.default_runtime_target = command_plan.runtime_target

    async def plan(self, **kwargs):
        return self.command_plan


def _docker_ready() -> tuple[bool, str]:
    try:
        version = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - environment-dependent
        return False, f"docker unavailable: {exc}"
    if version.returncode != 0:
        return False, version.stderr.strip() or version.stdout.strip() or "docker unavailable"

    inspect = subprocess.run(
        ["docker", "image", "inspect", DOCKER_SKILL_RUNTIME_IMAGE],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if inspect.returncode != 0:
        return False, f"missing docker image: {DOCKER_SKILL_RUNTIME_IMAGE}"
    return True, ""


def _workspace_mount_arg(path: Path) -> str:
    return f"{path.resolve().as_posix()}:/workspace"


def _build_docker_runtime_config(workspace_root: Path) -> KGAgentConfig:
    return KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=MCPConfig(
            servers=[
                MCPServerConfig(
                    name="skill-runtime",
                    command="docker",
                    stdio_framing="json_lines",
                    args=[
                        "run",
                        "--rm",
                        "-i",
                        "-e",
                        "MCP_SKILLS_DIR=/app/skills",
                        "-e",
                        "MCP_WORKSPACE_DIR=/workspace",
                        "-v",
                        _workspace_mount_arg(workspace_root),
                        DOCKER_SKILL_RUNTIME_IMAGE,
                        "python",
                        "/app/server.py",
                    ],
                    startup_timeout_s=20.0,
                    tool_timeout_s=40.0,
                    discover_tools=False,
                )
            ],
            capabilities=[],
        ),
        skill_runtime=SkillRuntimeConfig(server="skill-runtime"),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )


async def _wait_for_terminal_status(
    agent: AgentCore,
    run_id: str,
    *,
    timeout_s: float = 20.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await agent.get_skill_run_status(run_id=run_id)
        if status["run_status"] != "running":
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for docker skill run {run_id}")
        await asyncio.sleep(0.25)


def _workspace_dir_from_runtime_path(
    workspace_root: Path,
    runtime_workspace: str,
) -> Path:
    return workspace_root / PurePosixPath(runtime_workspace).name


@pytest.mark.asyncio
async def test_agent_core_can_execute_free_shell_generated_script_via_docker_runtime(
    tmp_path: Path,
):
    ready, reason = _docker_ready()
    if not ready:
        pytest.skip(reason)

    workspace_root = (tmp_path / "docker-workspace").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    config = _build_docker_runtime_config(workspace_root)
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="docker runtime skill route",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pdf",
            goal="Generate a helper script inside docker and execute it",
            reason="Matched local skill",
            constraints={
                "shell_mode": "free_shell",
                "shell_command": (
                    "python -c \"from pathlib import Path; "
                    "Path('docker_generated.txt').write_text("
                    "'docker free shell ok', encoding='utf-8')\""
                ),
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
            query="use the docker skill runtime and write a helper script first",
            session_id="docker-skill-runtime-session",
            use_memory=False,
            debug=True,
        )
        run_payload = response.tool_calls[0]["data"]
        assert run_payload["shell_mode"] == "free_shell"
        assert run_payload["runtime_target"]["platform"] == "linux"
        assert run_payload["command_plan"]["mode"] == "explicit"
        assert "python -c" in run_payload["command"]

        status = await _wait_for_terminal_status(agent, run_payload["run_id"])
        logs = await agent.get_skill_run_logs(run_id=run_payload["run_id"])
        artifacts = await agent.get_skill_run_artifacts(run_id=run_payload["run_id"])
    finally:
        await adapter.close()

    assert status["run_status"] == "completed"
    assert status["shell_mode"] == "free_shell"
    assert status["command_plan"]["mode"] == "explicit"
    assert status["runtime"]["delivery"] == "durable_worker"
    assert logs["run_status"] == "completed"
    assert artifacts["run_status"] == "completed"

    run_workspace = _workspace_dir_from_runtime_path(workspace_root, artifacts["workspace"])
    output_path = run_workspace / "docker_generated.txt"
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == "docker free shell ok"


@pytest.mark.asyncio
async def test_agent_core_can_execute_generated_script_bundle_via_docker_runtime(
    tmp_path: Path,
):
    ready, reason = _docker_ready()
    if not ready:
        pytest.skip(reason)

    workspace_root = (tmp_path / "docker-workspace-generated").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    config = _build_docker_runtime_config(workspace_root)
    adapter = MCPAdapter(config.mcp)
    skill_registry = SkillRegistry(Path("skills"))
    skill_loader = SkillLoader(skill_registry)
    command_plan = SkillCommandPlan(
        skill_name="pdf",
        goal="Write a helper script first and then execute it inside docker",
        user_query="write helper files first and then execute the generated entrypoint",
        runtime_target=SkillRuntimeTarget.linux_default(),
        constraints={"shell_mode": "free_shell"},
        command="python ./.skill_generated/write_bundle.py",
        mode="generated_script",
        shell_mode="free_shell",
        rationale="Materialize a generated helper script bundle before execution.",
        entrypoint=".skill_generated/write_bundle.py",
        cli_args=[],
        generated_files=[
            SkillGeneratedFile(
                path=".skill_generated/write_bundle.py",
                content=(
                    "from pathlib import Path\n"
                    "Path('docker_generated_bundle.txt').write_text("
                    "'docker generated bundle ok', encoding='utf-8')\n"
                    "print('docker generated script bundle ran')\n"
                ),
                description="Write a deterministic artifact for the docker runtime test.",
            )
        ],
        hints={"planner": "free_shell", "required_tools": ["python"]},
    )
    skill_executor = SkillExecutor(
        registry=skill_registry,
        loader=skill_loader,
        command_planner=_StaticCommandPlanner(command_plan),
        runtime_client=MCPBasedSkillRuntimeClient(
            adapter=adapter,
            config=config.skill_runtime,
        ),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=adapter,
        skill_registry=skill_registry,
        skill_loader=skill_loader,
        skill_executor=skill_executor,
    )

    try:
        invocation = await agent.invoke_skill(
            skill_name="pdf",
            session_id="docker-generated-script-session",
            goal="Write a helper script first and then execute it inside docker",
            query="write helper files first and then execute the generated entrypoint",
            constraints={"shell_mode": "free_shell"},
        )
        run_payload = invocation.result["data"]
        assert run_payload["shell_mode"] == "free_shell"
        assert run_payload["runtime_target"]["platform"] == "linux"
        assert run_payload["command_plan"]["mode"] == "generated_script"
        assert run_payload["command_plan"]["generated_files"][0]["path"] == (
            ".skill_generated/write_bundle.py"
        )

        status = await _wait_for_terminal_status(agent, run_payload["run_id"])
        logs = await agent.get_skill_run_logs(run_id=run_payload["run_id"])
        artifacts = await agent.get_skill_run_artifacts(run_id=run_payload["run_id"])
    finally:
        await adapter.close()

    assert status["run_status"] == "completed"
    assert status["shell_mode"] == "free_shell"
    assert status["command_plan"]["mode"] == "generated_script"
    assert status["runtime"]["delivery"] == "durable_worker"
    assert "docker generated script bundle ran" in logs["stdout"]
    assert any(item["path"] == "docker_generated_bundle.txt" for item in artifacts["artifacts"])

    run_workspace = _workspace_dir_from_runtime_path(workspace_root, artifacts["workspace"])
    output_path = run_workspace / "docker_generated_bundle.txt"
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == "docker generated bundle ok"
