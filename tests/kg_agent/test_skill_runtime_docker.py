from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

import pytest
from fastapi.testclient import TestClient

from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.route_judge import RouteDecision
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


def _docker_mount_arg(path: Path, target: str) -> str:
    return f"{path.resolve().as_posix()}:{target}"


def _runtime_host_dirs(root: Path) -> dict[str, Path]:
    return {
        "root": root.resolve(),
        "runs": (root / "runs").resolve(),
        "state": (root / "state").resolve(),
        "envs": (root / "envs").resolve(),
        "wheelhouse": (root / "wheelhouse").resolve(),
        "pip_cache": (root / "pip-cache").resolve(),
        "locks": (root / "locks").resolve(),
    }


def _build_docker_runtime_config(
    workspace_root: Path,
    *,
    mount_source: bool = True,
) -> KGAgentConfig:
    repo_root = Path(__file__).resolve().parents[2]
    dirs = _runtime_host_dirs(workspace_root)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    skills_dir = "/src/skills" if mount_source else "/app/skills"
    server_entrypoint = "/src/mcp-server/server.py" if mount_source else "/app/server.py"
    docker_args = [
        "run",
        "--rm",
        "-i",
        "-e",
        f"MCP_SKILLS_DIR={skills_dir}",
        "-e",
        "MCP_WORKSPACE_DIR=/workspace",
        "-e",
        "MCP_RUNS_DIR=/workspace/runs",
        "-e",
        "MCP_STATE_DIR=/workspace/state",
        "-e",
        "MCP_ENVS_DIR=/workspace/envs",
        "-e",
        "MCP_WHEELHOUSE_DIR=/workspace/wheelhouse",
        "-e",
        "MCP_PIP_CACHE_DIR=/workspace/pip-cache",
        "-e",
        "MCP_LOCKS_DIR=/workspace/locks",
    ]
    if mount_source:
        docker_args.extend(
            [
                "-e",
                "PYTHONPATH=/src",
                "-v",
                _docker_mount_arg(repo_root, "/src"),
            ]
        )
    docker_args.extend(
        [
            "-v",
            _docker_mount_arg(dirs["runs"], "/workspace/runs"),
            "-v",
            _docker_mount_arg(dirs["state"], "/workspace/state"),
            "-v",
            _docker_mount_arg(dirs["envs"], "/workspace/envs"),
            "-v",
            _docker_mount_arg(dirs["wheelhouse"], "/workspace/wheelhouse"),
            "-v",
            _docker_mount_arg(dirs["pip_cache"], "/workspace/pip-cache"),
            "-v",
            _docker_mount_arg(dirs["locks"], "/workspace/locks"),
            DOCKER_SKILL_RUNTIME_IMAGE,
            "python",
            server_entrypoint,
        ]
    )
    return KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=MCPConfig(
            servers=[
                MCPServerConfig(
                    name="skill-runtime",
                    command="docker",
                    stdio_framing="json_lines",
                    args=docker_args,
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
    runtime_workspace_path = PurePosixPath(runtime_workspace)
    try:
        relative_path = runtime_workspace_path.relative_to(PurePosixPath("/workspace"))
    except ValueError:
        relative_path = PurePosixPath(runtime_workspace_path.name)
    return (workspace_root / Path(*relative_path.parts)).resolve()


def _wait_for_terminal_status_via_api(
    client: TestClient,
    run_id: str,
    *,
    timeout_s: float = 20.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        response = client.get(f"/agent/skill-runs/{run_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["run_status"] != "running":
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for docker skill run {run_id}")
        time.sleep(0.25)


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


def test_agent_chat_endpoint_can_execute_skill_via_docker_runtime(
    tmp_path: Path,
):
    ready, reason = _docker_ready()
    if not ready:
        pytest.skip(reason)

    workspace_root = (tmp_path / "docker-workspace-api").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    config = _build_docker_runtime_config(workspace_root)
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="docker runtime skill route via /agent/chat",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pdf",
            goal="Generate a helper script inside docker and execute it from the chat API",
            reason="Matched local skill",
            constraints={
                "shell_mode": "free_shell",
                "shell_command": (
                    "python -c \"from pathlib import Path; "
                    "Path('docker_agent_chat.txt').write_text("
                    "'docker agent chat ok', encoding='utf-8')\""
                ),
            },
        ),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        mcp_adapter=MCPAdapter(config.mcp),
    )
    app = create_app(agent_core=agent)

    with TestClient(app) as client:
        chat_response = client.post(
            "/agent/chat",
            json={
                "query": "use the pdf skill runtime through the chat API",
                "session_id": "docker-agent-chat-session",
                "use_memory": False,
                "debug": True,
            },
        )
        assert chat_response.status_code == 200, chat_response.text
        chat_payload = chat_response.json()
        assert chat_payload["route"]["strategy"] == "skill_request"
        assert chat_payload["tool_calls"][0]["tool"] == "skill:pdf"
        assert chat_payload["tool_calls"][0]["success"] is True

        run_payload = chat_payload["tool_calls"][0]["data"]
        assert run_payload["shell_mode"] == "free_shell"
        assert run_payload["runtime_target"]["platform"] == "linux"
        assert run_payload["command_plan"]["mode"] == "explicit"
        assert "python -c" in run_payload["command"]

        status_payload = _wait_for_terminal_status_via_api(
            client,
            run_payload["run_id"],
        )
        logs_response = client.get(f"/agent/skill-runs/{run_payload['run_id']}/logs")
        artifacts_response = client.get(
            f"/agent/skill-runs/{run_payload['run_id']}/artifacts"
        )

    assert status_payload["run_status"] == "completed"
    assert status_payload["shell_mode"] == "free_shell"
    assert status_payload["command_plan"]["mode"] == "explicit"
    assert status_payload["runtime"]["delivery"] == "durable_worker"
    assert logs_response.status_code == 200
    logs_payload = logs_response.json()
    assert logs_payload["run_status"] == "completed"
    assert logs_payload["stdout"] == ""
    assert artifacts_response.status_code == 200
    artifacts_payload = artifacts_response.json()
    assert artifacts_payload["run_status"] == "completed"
    assert any(item["path"] == "docker_agent_chat.txt" for item in artifacts_payload["artifacts"])

    run_workspace = _workspace_dir_from_runtime_path(
        workspace_root,
        artifacts_payload["workspace"],
    )
    output_path = run_workspace / "docker_agent_chat.txt"
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == "docker agent chat ok"


def test_agent_chat_endpoint_can_execute_skill_via_rebuilt_docker_image(
    tmp_path: Path,
):
    ready, reason = _docker_ready()
    if not ready:
        pytest.skip(reason)

    workspace_root = (tmp_path / "docker-workspace-image").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    config = _build_docker_runtime_config(workspace_root, mount_source=False)
    route = RouteDecision(
        need_tools=False,
        need_memory=False,
        need_web_search=False,
        need_path_explanation=False,
        strategy="skill_request",
        tool_sequence=[],
        reason="rebuilt docker image skill route via /agent/chat",
        max_iterations=1,
        skill_plan=SkillPlan(
            skill_name="pdf",
            goal="Generate a helper script inside the rebuilt docker image and execute it from the chat API",
            reason="Matched local skill",
            constraints={
                "shell_mode": "free_shell",
                "shell_command": (
                    "python -c \"from pathlib import Path; "
                    "Path('docker_image_chat.txt').write_text("
                    "'docker rebuilt image ok', encoding='utf-8')\""
                ),
            },
        ),
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        route_judge=_StubRouteJudge(route=route),
        mcp_adapter=MCPAdapter(config.mcp),
    )
    app = create_app(agent_core=agent)

    with TestClient(app) as client:
        chat_response = client.post(
            "/agent/chat",
            json={
                "query": "use the pdf skill runtime through the rebuilt docker image",
                "session_id": "docker-image-agent-chat-session",
                "use_memory": False,
                "debug": True,
            },
        )
        assert chat_response.status_code == 200, chat_response.text
        chat_payload = chat_response.json()
        assert chat_payload["route"]["strategy"] == "skill_request"
        assert chat_payload["tool_calls"][0]["tool"] == "skill:pdf"
        assert chat_payload["tool_calls"][0]["success"] is True

        run_payload = chat_payload["tool_calls"][0]["data"]
        assert run_payload["shell_mode"] == "free_shell"
        assert run_payload["runtime_target"]["platform"] == "linux"
        assert run_payload["command_plan"]["mode"] == "explicit"
        assert "python -c" in run_payload["command"]

        status_payload = _wait_for_terminal_status_via_api(
            client,
            run_payload["run_id"],
        )
        artifacts_response = client.get(
            f"/agent/skill-runs/{run_payload['run_id']}/artifacts"
        )

    assert status_payload["run_status"] == "completed"
    assert status_payload["shell_mode"] == "free_shell"
    assert status_payload["command_plan"]["mode"] == "explicit"
    assert status_payload["runtime"]["delivery"] == "durable_worker"
    assert artifacts_response.status_code == 200
    artifacts_payload = artifacts_response.json()
    assert artifacts_payload["run_status"] == "completed"
    assert any(item["path"] == "docker_image_chat.txt" for item in artifacts_payload["artifacts"])

    run_workspace = _workspace_dir_from_runtime_path(
        workspace_root,
        artifacts_payload["workspace"],
    )
    output_path = run_workspace / "docker_image_chat.txt"
    assert output_path.is_file()
    assert output_path.read_text(encoding="utf-8") == "docker rebuilt image ok"


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
