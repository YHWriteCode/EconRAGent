from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import types
import uuid
from pathlib import Path

import pytest


def _load_runtime_service_module(*, tmp_path: Path, monkeypatch) -> object:
    skills_root = Path("skills").resolve()
    workspace_root = (tmp_path / "runtime-workspace").resolve()
    db_path = (tmp_path / "runtime-runs.sqlite3").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("MCP_WORKSPACE_DIR", str(workspace_root))
    monkeypatch.setenv("MCP_RUN_STORE_SQLITE_PATH", str(db_path))
    module_path = Path("mcp-server/server.py").resolve()
    module_name = f"skill_runtime_service_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load skill runtime service module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _materialize_ready_skill_env(module, *, skill_name: str) -> dict:
    loaded_skill = module._build_loaded_skill(skill_name)
    env_spec = module._build_skill_env_spec(loaded_skill)
    assert env_spec is not None
    env_dir = Path(env_spec["env_path"])
    bin_dir = Path(env_spec["bin_dir"])
    python_path = Path(env_spec["python_path"])
    bin_dir.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")
    module._write_json_atomic(
        module._skill_env_metadata_path(env_dir),
        {
            "format_version": module.ENV_HASH_FORMAT_VERSION,
            "env_hash": env_spec["env_hash"],
            "env_name": env_spec["env_name"],
            "dependency_file": env_spec["dependency_file"],
            "dependency_hash": env_spec["dependency_hash"],
            "created_at": module._utc_now(),
            "python_version": f"{module.sys.version_info.major}.{module.sys.version_info.minor}",
            "platform": module.platform.system().lower(),
            "machine": module.platform.machine().lower(),
        },
    )
    return env_spec


class _StubLLM:
    def __init__(self, payloads: list[dict] | dict):
        if isinstance(payloads, list):
            self.payloads = [dict(item) for item in payloads]
        else:
            self.payloads = [dict(payloads)]
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if not self.payloads:
            raise RuntimeError("No stub payload remaining")
        return self.payloads.pop(0)

    async def complete_text(self, **kwargs):
        self.calls.append(kwargs)
        if not self.payloads:
            raise RuntimeError("No stub payload remaining")
        return json.dumps(self.payloads.pop(0), ensure_ascii=False)


class _UnavailableLLM:
    def is_available(self) -> bool:
        return False


class _JsonFailThenTextSuccessLLM:
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.calls: list[dict] = []
        self.text_calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("json transport timed out")

    async def complete_text(self, **kwargs):
        self.text_calls.append(kwargs)
        return json.dumps(self.payload, ensure_ascii=False)


class _JsonAndTextFailLLM:
    def __init__(self):
        self.calls: list[dict] = []
        self.text_calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("json transport timed out")

    async def complete_text(self, **kwargs):
        self.text_calls.append(kwargs)
        raise RuntimeError("text transport timed out")


class _MalformedTextThenCompactRepairLLM:
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.calls: list[dict] = []
        self.text_calls: list[dict] = []
        self._repair_sent = False

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("json transport timed out")

    async def complete_text(self, **kwargs):
        self.text_calls.append(kwargs)
        if not self._repair_sent:
            self._repair_sent = True
            return (
                '{'
                '"mode":"generated_script",'
                '"entrypoint":".skill_generated/create_presentation.py",'
                '"cli_args":[],'
                '"generated_files":[{"path":".skill_generated/create_presentation.py",'
                '"content":"#!/usr/bin/env python3\\nprint(\\"hello'
            )
        return json.dumps(self.payload, ensure_ascii=False)


def test_runtime_service_uses_non_login_shell_for_posix_exec(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module._build_shell_exec_argv.__globals__["os"], "name", "posix")

    argv = module._build_shell_exec_argv("python scripts/analyze_stock_trend.py")

    assert argv == ["/bin/sh", "-c", "python scripts/analyze_stock_trend.py"]


def test_runtime_service_materializes_generated_js_entrypoint_with_node(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    loaded_skill = module._build_loaded_skill("pptx")
    workspace_dir = (tmp_path / "generated-js-workspace").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    command = module._materialize_shell_command(
        loaded_skill=loaded_skill,
        command_plan=module.SkillCommandPlan(
            skill_name="pptx",
            goal="Create a PPT",
            user_query="make a deck",
            runtime_target=module.DEFAULT_RUNTIME_TARGET,
            mode="generated_script",
            entrypoint=".skill_generated/create_ppt.js",
            cli_args=["--topic", "科幻小说起源发展"],
        ),
        workspace_dir=workspace_dir,
    )

    assert command is not None
    assert "node" in command.lower()
    assert str((workspace_dir / ".skill_generated" / "create_ppt.js").resolve()) in command
    assert "--topic" in command


def test_runtime_service_utility_llm_client_falls_back_to_main_model_env(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("KG_AGENT_UTILITY_MODEL_NAME", raising=False)
    monkeypatch.delenv("KG_AGENT_UTILITY_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("KG_AGENT_UTILITY_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("UTILITY_LLM_MODEL", raising=False)
    monkeypatch.delenv("UTILITY_LLM_BINDING_HOST", raising=False)
    monkeypatch.delenv("UTILITY_LLM_BINDING_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODEL", "fallback-main")
    monkeypatch.setenv("LLM_BINDING_HOST", "http://main.local/v1")
    monkeypatch.setenv("LLM_BINDING_API_KEY", "fallback-key")

    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    client = module.UTILITY_LLM_CLIENT
    assert client.is_available() is True
    assert client.primary is None
    assert client.fallback is not None
    assert client.fallback.config.model_name == "fallback-main"
    assert client.fallback.config.base_url == "http://main.local/v1"


def test_runtime_service_build_run_env_reuses_cached_skill_env_for_shipped_script(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setitem(
        module._build_run_env.__globals__,
        "_skill_env_has_required_distributions",
        lambda **kwargs: True,
    )
    loaded_skill = module._build_loaded_skill("xlsx")
    env_spec = _materialize_ready_skill_env(module, skill_name="xlsx")
    workspace_dir = (tmp_path / "workspace-for-env").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    env, resolved_spec = module._build_run_env(
        skill_dir=loaded_skill.skill.path.resolve(),
        workspace_dir=workspace_dir,
        goal="Recalculate workbook",
        user_query="recalc workbook",
        constraints={},
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        request_file=workspace_dir / "skill_request.json",
        loaded_skill=loaded_skill,
        command_plan=module.SkillCommandPlan(
            skill_name="xlsx",
            goal="Recalculate workbook",
            user_query="recalc workbook",
            runtime_target=module.DEFAULT_RUNTIME_TARGET,
            entrypoint="scripts/recalc.py",
            mode="inferred",
        ),
        materialize_skill_env=False,
    )

    assert resolved_spec is not None
    assert resolved_spec["env_hash"] == env_spec["env_hash"]
    assert resolved_spec["ready"] is True
    assert resolved_spec["reused"] is True
    assert env["VIRTUAL_ENV"] == env_spec["env_path"]
    assert env["SKILL_PYTHON_BIN"] == env_spec["python_path"]
    assert env["PATH"].split(os.pathsep)[0] == env_spec["bin_dir"]
    assert '"ready": true' in env["SKILL_ENVIRONMENT_JSON"].lower()


def test_runtime_service_marks_cached_skill_env_not_ready_when_dependency_check_fails(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    env_spec = _materialize_ready_skill_env(module, skill_name="xlsx")
    monkeypatch.setitem(
        module._build_run_env.__globals__,
        "_skill_env_has_required_distributions",
        lambda **kwargs: False,
    )

    assert module._build_run_env.__globals__["_skill_env_is_ready"](env_spec) is False


def test_runtime_service_skill_env_install_keeps_venv_python_path(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setitem(
        module._build_run_env.__globals__,
        "_runtime_bin_dir_name",
        lambda: "bin",
    )
    monkeypatch.setitem(
        module._build_run_env.__globals__,
        "_runtime_python_name",
        lambda: "python",
    )
    loaded_skill = module._build_loaded_skill("financial-researching")
    env_spec = module._build_skill_env_spec(loaded_skill)
    assert env_spec is not None
    real_python = (tmp_path / "real-python").resolve()
    real_python.write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    completed_process = module._build_run_env.__globals__["subprocess"].CompletedProcess

    def _fake_run(argv, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:3] == [module.sys.executable, "-m", "venv"]:
            temp_env_dir = Path(command[3]).resolve()
            bin_dir = temp_env_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            python_path = bin_dir / "python"
            try:
                os.symlink(real_python, python_path)
            except (NotImplementedError, OSError):
                pytest.skip("symlink creation is not available in this test environment")
            return completed_process(command, 0, "", "")
        if len(command) >= 4 and command[1:4] == ["-m", "pip", "install"]:
            return completed_process(command, 0, "", "")
        if command[:4] == [module.sys.executable, "-m", "pip", "download"]:
            return completed_process(command, 0, "", "")
        raise AssertionError(f"Unexpected subprocess argv: {command}")

    monkeypatch.setattr(module._build_run_env.__globals__["subprocess"], "run", _fake_run)

    resolved_spec = module._build_run_env.__globals__["_ensure_skill_env_ready"](
        env_spec,
        allow_network=True,
    )

    expected_python = str(Path(resolved_spec["env_path"]) / "bin" / "python")
    pip_install_commands = [
        command for command in calls if len(command) >= 4 and command[1:4] == ["-m", "pip", "install"]
    ]
    assert pip_install_commands
    assert pip_install_commands[0][0] == expected_python
    assert resolved_spec["python_path"] == expected_python


def test_runtime_service_build_run_env_falls_back_to_online_install_when_wheelhouse_misses(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    loaded_skill = module._build_loaded_skill("financial-researching")
    workspace_dir = (tmp_path / "workspace-online-fallback").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    completed_process = module._build_run_env.__globals__["subprocess"].CompletedProcess

    def _fake_run(argv, **kwargs):
        command = [str(item) for item in argv]
        calls.append(command)
        if command[:3] == [module.sys.executable, "-m", "venv"]:
            temp_env_dir = Path(command[3]).resolve()
            bin_dir = temp_env_dir / ("Scripts" if os.name == "nt" else "bin")
            python_name = "python.exe" if os.name == "nt" else "python"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / python_name).write_text("", encoding="utf-8")
            return completed_process(command, 0, "", "")
        if "--no-index" in command:
            return completed_process(command, 1, "", "wheelhouse miss")
        if command[:4] == [module.sys.executable, "-m", "pip", "download"]:
            return completed_process(command, 0, "", "")
        if command[1:4] == ["-m", "pip", "install"]:
            return completed_process(command, 0, "", "")
        raise AssertionError(f"Unexpected subprocess argv: {command}")

    monkeypatch.setattr(module._build_run_env.__globals__["subprocess"], "run", _fake_run)

    env, resolved_spec = module._build_run_env(
        skill_dir=loaded_skill.skill.path.resolve(),
        workspace_dir=workspace_dir,
        goal="Analyze Tesla trend",
        user_query="analyze tsla",
        constraints={},
        runtime_target=module.SkillRuntimeTarget.from_dict(
            {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": True,
                "supports_python": True,
            }
        ),
        request_file=workspace_dir / "skill_request.json",
        loaded_skill=loaded_skill,
        command_plan=module.SkillCommandPlan(
            skill_name="financial-researching",
            goal="Analyze Tesla trend",
            user_query="analyze tsla",
            runtime_target=module.DEFAULT_RUNTIME_TARGET,
            entrypoint="scripts/analyze_stock_trend.py",
            mode="inferred",
        ),
        materialize_skill_env=True,
    )

    assert resolved_spec is not None
    assert resolved_spec["ready"] is True
    assert resolved_spec["materialized"] is True
    assert resolved_spec["reused"] is False
    assert env["VIRTUAL_ENV"] == resolved_spec["env_path"]
    assert env["SKILL_PYTHON_BIN"] == resolved_spec["python_path"]
    assert any("--no-index" in command for command in calls)
    assert any(
        command[1:4] == ["-m", "pip", "install"] and "--no-index" not in command
        for command in calls
    )
    assert any(command[:4] == [module.sys.executable, "-m", "pip", "download"] for command in calls)


def test_runtime_service_build_run_env_reuses_bootstrap_node_runtime_and_node_path(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    loaded_skill = module._build_loaded_skill("pptx")
    workspace_dir = (tmp_path / "workspace-node-runtime").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    request_file = workspace_dir / "skill_invocation.json"

    initial_env, _ = module._build_run_env(
        skill_dir=loaded_skill.skill.path.resolve(),
        workspace_dir=workspace_dir,
        goal="Create a PPT",
        user_query="生成一个关于科幻小说起源发展的PPT",
        constraints={"shell_mode": "free_shell"},
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        request_file=request_file,
        loaded_skill=loaded_skill,
        command_plan=module.SkillCommandPlan(
            skill_name="pptx",
            goal="Create a PPT",
            user_query="生成一个关于科幻小说起源发展的PPT",
            runtime_target=module.DEFAULT_RUNTIME_TARGET,
            mode="generated_script",
            shell_mode="free_shell",
            entrypoint=".skill_generated/create_presentation.py",
            bootstrap_commands=["npm install -g pptxgenjs"],
            hints={"planner": "free_shell", "required_tools": ["python", "node", "npm"]},
        ),
        materialize_skill_env=False,
    )

    bootstrap_root = Path(initial_env["SKILL_BOOTSTRAP_ROOT"]).resolve()
    platform_tag = "win-x64" if os.name == "nt" else "linux-x64"
    node_runtime_root = (bootstrap_root / ".node-runtime" / f"node-v20.19.5-{platform_tag}").resolve()
    node_bin_dir = node_runtime_root if os.name == "nt" else (node_runtime_root / "bin").resolve()
    node_bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (node_bin_dir / "node.exe").write_text("", encoding="utf-8")
        (node_bin_dir / "npm.cmd").write_text("", encoding="utf-8")
    else:
        (node_bin_dir / "node").write_text("", encoding="utf-8")
        (node_bin_dir / "npm").write_text("", encoding="utf-8")
    node_modules_dir = (bootstrap_root / "lib" / "node_modules").resolve()
    node_modules_dir.mkdir(parents=True, exist_ok=True)

    env, _ = module._build_run_env(
        skill_dir=loaded_skill.skill.path.resolve(),
        workspace_dir=workspace_dir,
        goal="Create a PPT",
        user_query="生成一个关于科幻小说起源发展的PPT",
        constraints={"shell_mode": "free_shell"},
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        request_file=request_file,
        loaded_skill=loaded_skill,
        command_plan=module.SkillCommandPlan(
            skill_name="pptx",
            goal="Create a PPT",
            user_query="生成一个关于科幻小说起源发展的PPT",
            runtime_target=module.DEFAULT_RUNTIME_TARGET,
            mode="generated_script",
            shell_mode="free_shell",
            entrypoint=".skill_generated/create_presentation.py",
            bootstrap_commands=["npm install -g pptxgenjs"],
            hints={"planner": "free_shell", "required_tools": ["python", "node", "npm"]},
        ),
        materialize_skill_env=False,
    )

    path_entries = env["PATH"].split(os.pathsep)
    assert str(node_bin_dir) in path_entries
    assert env["NODE_PATH"].split(os.pathsep)[0] == str(node_modules_dir)
    assert env["NPM_CONFIG_CACHE"].startswith(str(bootstrap_root))
    assert env["npm_config_cache"].startswith(str(bootstrap_root))


def test_materialize_shell_command_rewrites_workspace_root_cli_args(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    loaded_skill = module._build_loaded_skill("financial-researching")
    workspace_dir = (tmp_path / "workspace-materialize").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    fake_runtime_root = (tmp_path / "container-workspace").resolve()

    command_plan = module.SkillCommandPlan(
        skill_name="financial-researching",
        goal="Analyze Tesla trend",
        user_query="analyze tsla",
        runtime_target=module.SkillRuntimeTarget.from_dict(
            {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": fake_runtime_root.as_posix(),
                "workdir": fake_runtime_root.as_posix(),
                "network_allowed": True,
                "supports_python": True,
            }
        ),
        constraints={},
        command=(
            "python scripts/analyze_stock_trend.py --code TSLA --start 20251018 "
            "--end 20260418 --trend-start 20251018 --trend-end 20260418 "
            f"--output {fake_runtime_root.as_posix()}/output/tsla_trend.json"
        ),
        mode="inferred",
        shell_mode="conservative",
        entrypoint="scripts/analyze_stock_trend.py",
        cli_args=[
            "--code",
            "TSLA",
            "--start",
            "20251018",
            "--end",
            "20260418",
            "--trend-start",
            "20251018",
            "--trend-end",
            "20260418",
            "--output",
            f"{fake_runtime_root.as_posix()}/output/tsla_trend.json",
        ],
    )

    command = module._materialize_shell_command(
        loaded_skill=loaded_skill,
        command_plan=command_plan,
        workspace_dir=workspace_dir,
    )

    assert command is not None
    assert f"{fake_runtime_root.as_posix()}/output/tsla_trend.json" not in command
    assert str((workspace_dir / "output" / "tsla_trend.json")).replace("\\", "/") in (
        command.replace("\\", "/")
    )


def test_materialize_shell_command_rewrites_workspace_output_into_shared_output_dir(
    tmp_path: Path,
    monkeypatch,
):
    shared_output_root = (tmp_path / "skill_output").resolve()
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(shared_output_root))
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    loaded_skill = module._build_loaded_skill("financial-researching")
    workspace_dir = (tmp_path / "workspace-shared-output" / "run-42").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    fake_runtime_root = (tmp_path / "container-workspace").resolve()

    command_plan = module.SkillCommandPlan(
        skill_name="financial-researching",
        goal="Analyze Tesla trend",
        user_query="analyze tsla",
        runtime_target=module.SkillRuntimeTarget.from_dict(
            {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": fake_runtime_root.as_posix(),
                "workdir": fake_runtime_root.as_posix(),
                "network_allowed": True,
                "supports_python": True,
            }
        ),
        constraints={},
        command=(
            "python scripts/analyze_stock_trend.py --code TSLA "
            f"--output {fake_runtime_root.as_posix()}/output/tsla_trend.json"
        ),
        mode="inferred",
        shell_mode="conservative",
        entrypoint="scripts/analyze_stock_trend.py",
        cli_args=[
            "--code",
            "TSLA",
            "--output",
            f"{fake_runtime_root.as_posix()}/output/tsla_trend.json",
        ],
    )

    command = module._materialize_shell_command(
        loaded_skill=loaded_skill,
        command_plan=command_plan,
        workspace_dir=workspace_dir,
    )

    assert command is not None
    expected_output = shared_output_root / workspace_dir.name / "tsla_trend.json"
    assert str(expected_output).replace("\\", "/") in command.replace("\\", "/")
    assert f"{fake_runtime_root.as_posix()}/output/tsla_trend.json" not in command


async def _wait_for_terminal_run(module, run_id: str, *, timeout_s: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = module.get_run_status(run_id)
        if status["run_status"] != "running":
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for run {run_id} to leave running state")
        await asyncio.sleep(0.05)


async def _wait_for_log_text(
    module,
    run_id: str,
    needle: str,
    *,
    timeout_s: float = 3.0,
):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        logs = module.get_run_logs(run_id)
        if needle in logs.get("stdout", "") or needle in logs.get("stderr", ""):
            return logs
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for log text {needle!r} in run {run_id}")
        await asyncio.sleep(0.05)


async def _wait_for_artifact_path(
    module,
    run_id: str,
    path: str,
    *,
    timeout_s: float = 3.0,
):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        artifacts = module.get_run_artifacts(run_id)
        if any(item.get("path") == path for item in artifacts.get("artifacts", [])):
            return artifacts
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Timed out waiting for artifact {path!r} in run {run_id}"
            )
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_runtime_service_can_infer_and_execute_single_script_skill(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Quarterly Outlook",
        user_query="Include macro context",
        constraints={},
        wait_for_completion=True,
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["status"] == "completed"
    assert result["execution_mode"] == "shell"
    assert result["shell_mode"] == "conservative"
    assert result["runtime_target"]["platform"] == "linux"
    assert result["runtime"]["total_duration_s"] is not None
    assert result["runtime"]["execution_duration_s"] is not None
    assert "run_report.py" in result["command"]
    assert result["command_plan"]["mode"] == "inferred"
    assert result["command_plan"]["entrypoint"] == "scripts/run_report.py"
    assert result["started_at"]
    assert result["finished_at"]
    assert result["exit_code"] == 0
    assert result["artifacts"][0]["path"] == "report.md"

    report_path = Path(result["workspace"]) / "report.md"
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "# Quarterly Outlook" in report_text
    assert "Include macro context" in report_text

    status = module.get_run_status(result["run_id"])
    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])
    assert status["run_status"] == "completed"
    assert status["status"] == "completed"
    assert status["runtime_target"]["platform"] == "linux"
    assert status["preflight"]["ok"] is True
    assert status["command_plan"]["mode"] == "inferred"
    assert logs["stdout"]
    assert logs["run_status"] == "completed"
    assert logs["runtime_target"]["platform"] == "linux"
    assert artifacts["artifacts"][0]["path"] == "report.md"
    assert artifacts["run_status"] == "completed"
    assert artifacts["runtime_target"]["platform"] == "linux"


@pytest.mark.asyncio
async def test_runtime_service_returns_shell_plan_for_complex_skill_without_command(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())

    result = await module.run_skill_task(
        skill_name="xlsx",
        goal="Fix the spreadsheet and preserve formulas",
        user_query="repair this workbook",
        constraints={},
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["status"] == "needs_shell_command"
    assert result["execution_mode"] == "shell"
    assert result["runtime_target"]["platform"] == "linux"
    assert result["command_plan"]["mode"] == "manual_required"
    assert "runnable_scripts" not in result["command_plan"]["hints"]

    status = module.get_run_status(result["run_id"])
    assert status["run_status"] == "manual_required"
    assert status["status"] == "needs_shell_command"
    assert status["failure_reason"] == result["failure_reason"]


@pytest.mark.asyncio
async def test_runtime_service_reports_llm_unavailable_for_pptx_when_free_shell_llm_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())

    result = await module.run_skill_task(
        skill_name="pptx",
        goal="创建一个关于生成式人工智能起源发展的演示文稿",
        user_query="请生成一个关于生成式人工智能起源发展的PPT",
        constraints={"topic": "生成式人工智能起源发展", "output_format": "pptx"},
        wait_for_completion=True,
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["failure_reason"] == "llm_not_available_for_free_shell"
    assert result["command_plan"]["mode"] == "manual_required"
    assert result["shell_mode_requested"] == "conservative"
    assert result["shell_mode_effective"] == "free_shell"
    assert result["shell_mode_escalated"] is True
    assert result["planning_blockers"][0]["failure_reason"] == "manual_command_required"
    assert result["command_plan"]["hints"]["planner"] == "free_shell"
    assert result["manual_required_kind"] == "technical_blocked"
    assert "free-shell planning" in str(result["planner_error_summary"]).lower()
    assert "technical planning blocker" in str(result["notes"]).lower()
    assert "file_inventory" not in result
    assert "shell_hints" not in result
    assert "references" not in result
    assert "runnable_scripts" not in result["command_plan"]["hints"]
    assert isinstance(result["command_plan"]["hints"]["planner_error_summary"], str)
    assert all(
        isinstance(item, str) for item in result["command_plan"]["hints"].get("warnings", [])
    )
    assert isinstance(result["planning_blockers"][0]["rationale"], str)
    assert result["artifacts"] == []


@pytest.mark.asyncio
async def test_runtime_service_can_execute_micro_context_text_first_plan(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        module,
        "UTILITY_LLM_CLIENT",
        _JsonFailThenTextSuccessLLM(
            {
                "mode": "generated_script",
                "command": None,
                "entrypoint": ".skill_generated/main.py",
                "cli_args": [],
                "generated_files": [
                    {
                        "path": ".skill_generated/main.py",
                        "content": "print('build pptx deck')\n",
                        "description": "Generated PPTX workflow entrypoint.",
                    }
                ],
                "rationale": "Recovered with micro-context text planning.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            }
        ),
    )

    result = await module.run_skill_task(
        skill_name="pptx",
        goal="创建一个关于生成式人工智能起源发展的演示文稿",
        user_query="请生成一个关于生成式人工智能起源发展的PPT",
        constraints={"topic": "生成式人工智能起源发展", "output_format": "pptx"},
        wait_for_completion=True,
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["failure_reason"] is None
    assert result["command_plan"]["mode"] == "generated_script"
    assert result["command_plan"]["hints"]["planner"] == "free_shell"
    assert result["command_plan"]["hints"]["planner_context_mode"] == "micro_context"
    assert result["command_plan"]["hints"]["planner_transport"] == "text_first"
    assert result["planner_context_mode"] == "micro_context"
    assert result["planner_transport"] == "text_first"
    assert len(result["command_plan"]["hints"]["planner_attempts"]) == 4


@pytest.mark.asyncio
async def test_runtime_service_reports_planning_failure_only_after_all_free_shell_attempts(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _JsonAndTextFailLLM())

    result = await module.run_skill_task(
        skill_name="pptx",
        goal="创建一个关于生成式人工智能起源发展的演示文稿",
        user_query="请生成一个关于生成式人工智能起源发展的PPT",
        constraints={"topic": "生成式人工智能起源发展", "output_format": "pptx"},
        wait_for_completion=True,
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["failure_reason"] == "llm_planning_failed"
    assert result["manual_required_kind"] == "technical_blocked"
    assert result["command_plan"]["hints"]["planner"] == "free_shell"
    assert result["command_plan"]["hints"]["planner_context_mode"] == "micro_context"
    assert result["command_plan"]["hints"]["planner_transport"] == "text_first"
    assert len(result["command_plan"]["hints"]["planner_attempts"]) == 4
    assert "deterministic_pptx_fallback" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_runtime_service_can_compact_repair_truncated_text_first_plan(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        module,
        "UTILITY_LLM_CLIENT",
        _MalformedTextThenCompactRepairLLM(
            {
                "mode": "generated_script",
                "command": None,
                "entrypoint": ".skill_generated/main.py",
                "cli_args": [],
                "generated_files": [
                    {
                        "path": ".skill_generated/main.py",
                        "content": "print('compact repair')\n",
                        "description": "Compact repaired PPTX workflow entrypoint.",
                    }
                ],
                "rationale": "Recovered by compacting a truncated generated-script plan.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            }
        ),
    )

    result = await module.run_skill_task(
        skill_name="pptx",
        goal="创建一个关于科幻小说起源发展的演示文稿",
        user_query="请生成一个关于科幻小说起源发展的PPT",
        constraints={"topic": "科幻小说起源发展", "output_format": "pptx"},
        wait_for_completion=True,
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["failure_reason"] is None
    assert result["command_plan"]["mode"] == "generated_script"
    assert result["command_plan"]["hints"]["planner"] == "free_shell"
    assert result["command_plan"]["hints"]["planner_context_mode"] == "micro_context"
    assert result["command_plan"]["hints"]["planner_transport"] == "text_first"


@pytest.mark.asyncio
async def test_runtime_service_still_reports_llm_unavailable_when_no_fallback_exists(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())

    result = await module.run_skill_task(
        skill_name="pptx",
        goal="Create a deck from a provided template",
        user_query="make a branded ppt deck",
        constraints={"output_format": "pptx", "template": "brand-template.pptx"},
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["failure_reason"] == "llm_not_available_for_free_shell"
    assert result["shell_mode_effective"] == "free_shell"
    assert result["shell_mode_escalated"] is True
    assert result["manual_required_kind"] == "technical_blocked"


@pytest.mark.asyncio
async def test_runtime_service_builds_command_from_structured_args(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Ignored by structured args",
        user_query="custom report",
        constraints={
            "args": {
                "topic": "Board Update",
                "notes": "Use the structured args path.",
            }
        },
        wait_for_completion=True,
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["command_plan"]["mode"] == "structured_args"
    assert "--topic" in result["command"]
    report_text = (Path(result["workspace"]) / "report.md").read_text(encoding="utf-8")
    assert "# Board Update" in report_text
    assert "Use the structured args path." in report_text


@pytest.mark.asyncio
async def test_runtime_service_can_plan_xlsx_recalc_without_execution(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="xlsx",
        goal="Recalculate spreadsheet formulas",
        user_query='recalculate formulas in "C:\\Reports\\model.xlsx"',
        constraints={
            "dry_run": True,
            "operation": "recalc",
            "input_path": "C:\\Reports\\model.xlsx",
        },
    )

    assert result["success"] is True
    assert result["run_id"]
    assert result["run_status"] == "planned"
    assert result["status"] == "planned"
    assert result["execution_mode"] == "shell"
    assert result["runtime_target"]["platform"] == "linux"
    assert result["command_plan"]["mode"] == "inferred"
    assert "scripts/recalc.py" in result["command"].replace("\\", "/")
    assert "model.xlsx" in result["command"]
    assert result["started_at"]
    assert result["finished_at"]

    status = module.get_run_status(result["run_id"])
    assert status["run_status"] == "planned"
    assert status["status"] == "planned"
    assert status["command_plan"]["entrypoint"] == "scripts/recalc.py"


@pytest.mark.asyncio
async def test_runtime_service_fails_cleanly_when_skill_env_wheelhouse_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="financial-researching",
        goal="Show financial skill help",
        user_query="show the bundled script help",
        constraints={},
        wait_for_completion=True,
        command_plan={
            "skill_name": "financial-researching",
            "goal": "Show financial skill help",
            "user_query": "show the bundled script help",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": "python scripts/analyze_stock_trend.py --help",
            "mode": "explicit",
            "shell_mode": "conservative",
            "rationale": "Run a shipped skill script that requires the managed skill env.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"required_tools": ["python"]},
        },
    )

    assert result["success"] is False
    assert result["run_status"] == "failed"
    assert result["failure_reason"] == "skill_env_unavailable"
    assert "wheelhouse" in result["logs"]["stderr"].lower()
    assert result["runtime"]["skill_environment"]["enabled"] is True
    assert result["runtime"]["skill_environment"]["requires_materialization"] is True
    assert result["runtime"]["skill_environment"]["dependency_file"] == "requirements.lock"


@pytest.mark.asyncio
async def test_runtime_service_rewrites_absolute_workspace_root_outputs_into_run_workspace(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    fake_runtime_root = (tmp_path / "container-workspace").resolve()
    fake_output_path = fake_runtime_root / "output" / "rewritten.txt"

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Write the report into the runtime workspace output directory",
        user_query="store output in the shared workspace output path",
        constraints={},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Write the report into the runtime workspace output directory",
            "user_query": "store output in the shared workspace output path",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": fake_runtime_root.as_posix(),
                "workdir": fake_runtime_root.as_posix(),
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": (
                f"New-Item -ItemType Directory -Force -Path '{fake_output_path.parent.as_posix()}' | Out-Null; "
                f"Set-Content -LiteralPath '{fake_output_path.as_posix()}' "
                "-Value 'rewritten ok'"
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "rationale": "Exercise workspace-root rewriting for explicit commands.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
    )

    expected_output = Path(result["workspace"]) / "output" / "rewritten.txt"

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert expected_output.read_text(encoding="utf-8").strip() == "rewritten ok"
    assert fake_output_path.exists() is False
    assert "output/rewritten.txt" in {item["path"] for item in result["artifacts"]}


@pytest.mark.asyncio
async def test_runtime_service_collects_shared_output_artifacts_when_configured(
    tmp_path: Path,
    monkeypatch,
):
    shared_output_root = (tmp_path / "skill_output").resolve()
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(shared_output_root))
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    fake_runtime_root = (tmp_path / "container-workspace-shared").resolve()
    fake_output_path = fake_runtime_root / "output" / "rewritten.txt"

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Write the report into the shared output directory",
        user_query="store output in the shared output path",
        constraints={},
        command_plan={
            "skill_name": "example-skill",
            "goal": "Write the report into the shared output directory",
            "user_query": "store output in the shared output path",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": fake_runtime_root.as_posix(),
                "workdir": fake_runtime_root.as_posix(),
                "network_allowed": True,
                "supports_python": False,
            },
            "constraints": {},
            "command": (
                f"New-Item -ItemType Directory -Force -Path '{fake_output_path.parent.as_posix()}' | Out-Null; "
                f"Set-Content -LiteralPath '{fake_output_path.as_posix()}' "
                "-Value 'shared output ok'"
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "entrypoint": None,
            "cli_args": [],
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
        wait_for_completion=True,
    )

    run_workspace = Path(result["workspace"]).resolve()
    expected_output = shared_output_root / run_workspace.name / "rewritten.txt"
    assert expected_output.is_file()
    assert expected_output.read_text(encoding="utf-8").strip() == "shared output ok"
    assert fake_output_path.exists() is False
    assert "output/rewritten.txt" in {item["path"] for item in result["artifacts"]}


@pytest.mark.asyncio
async def test_runtime_service_mirrors_run_local_output_dir_into_shared_output_root(
    tmp_path: Path,
    monkeypatch,
):
    shared_output_root = (tmp_path / "skill_output").resolve()
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(shared_output_root))
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Write the report into the run-local output directory",
        user_query="store output in output/",
        constraints={},
        command_plan={
            "skill_name": "example-skill",
            "goal": "Write the report into the run-local output directory",
            "user_query": "store output in output/",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": True,
                "supports_python": False,
            },
            "constraints": {},
            "command": (
                "New-Item -ItemType Directory -Force -Path 'output' | Out-Null; "
                "Set-Content -LiteralPath 'output/rewritten.txt' -Value 'mirrored output ok'"
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "entrypoint": None,
            "cli_args": [],
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
        wait_for_completion=True,
    )

    run_workspace = Path(result["workspace"]).resolve()
    local_output = run_workspace / "output" / "rewritten.txt"
    mirrored_output = shared_output_root / run_workspace.name / "rewritten.txt"

    assert local_output.is_file()
    assert local_output.read_text(encoding="utf-8").strip() == "mirrored output ok"
    assert mirrored_output.is_file()
    assert mirrored_output.read_text(encoding="utf-8").strip() == "mirrored output ok"
    assert [item["path"] for item in result["artifacts"]].count("output/rewritten.txt") == 1


@pytest.mark.asyncio
async def test_runtime_service_does_not_double_rewrite_shared_output_paths_under_workspace_root(
    tmp_path: Path,
    monkeypatch,
):
    fake_runtime_root = (tmp_path / "container-workspace-shared").resolve()
    shared_output_root = (fake_runtime_root / "output").resolve()
    monkeypatch.setenv("MCP_OUTPUT_DIR", str(shared_output_root))
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    fake_output_path = fake_runtime_root / "output" / "rewritten.txt"

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Write the report into the shared output directory under the runtime workspace",
        user_query="store output in the shared output path",
        constraints={},
        command_plan={
            "skill_name": "example-skill",
            "goal": "Write the report into the shared output directory under the runtime workspace",
            "user_query": "store output in the shared output path",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": fake_runtime_root.as_posix(),
                "workdir": fake_runtime_root.as_posix(),
                "network_allowed": True,
                "supports_python": False,
            },
            "constraints": {},
            "command": (
                f"New-Item -ItemType Directory -Force -Path '{fake_output_path.parent.as_posix()}' | Out-Null; "
                f"Set-Content -LiteralPath '{fake_output_path.as_posix()}' "
                "-Value 'shared output ok'"
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "entrypoint": None,
            "cli_args": [],
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
        wait_for_completion=True,
    )

    run_workspace = Path(result["workspace"]).resolve()
    expected_output = shared_output_root / run_workspace.name / "rewritten.txt"
    unexpected_nested_output = run_workspace / "output" / run_workspace.name / "rewritten.txt"

    assert expected_output.is_file()
    assert expected_output.read_text(encoding="utf-8").strip() == "shared output ok"
    assert fake_output_path.exists() is False
    assert unexpected_nested_output.exists() is False
    assert "output/rewritten.txt" in {item["path"] for item in result["artifacts"]}


@pytest.mark.asyncio
async def test_runtime_service_can_execute_generated_script_from_command_plan(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Run generated helper script",
        user_query="free shell execution",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Run generated helper script",
            "user_query": "free shell execution",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python ./.skill_generated/echo.py",
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "rationale": "Use a generated helper script.",
            "generated_files": [
                {
                    "path": ".skill_generated/echo.py",
                    "content": (
                        "from pathlib import Path\n"
                        "Path('generated.txt').write_text('free-shell ok', encoding='utf-8')\n"
                        "print('generated script ran')\n"
                    ),
                    "description": "Test helper",
                }
            ],
            "cli_args": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["command_plan"]["mode"] == "generated_script"
    assert result["command_plan"]["shell_mode"] == "free_shell"
    assert result["runtime_target"]["platform"] == "linux"
    assert ".skill_generated/echo.py" in result["generated_files"]
    generated_output = Path(result["workspace"]) / "generated.txt"
    assert generated_output.read_text(encoding="utf-8") == "free-shell ok"


@pytest.mark.asyncio
async def test_runtime_service_generated_script_can_reference_skill_scripts_relative_to_workspace(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Run a generated script that shells out to a mirrored skill script",
        user_query="write helper files first and then execute a generated entrypoint",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "example-skill",
            "goal": "Run a generated script that shells out to a mirrored skill script",
            "user_query": "write helper files first and then execute a generated entrypoint",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python ./.skill_generated/main.py",
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "entrypoint": ".skill_generated/main.py",
            "rationale": "Use a generated wrapper that invokes scripts/run_report.py from the mirrored skill workspace.",
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": (
                        "import subprocess\n"
                        "import sys\n"
                        "subprocess.run([\n"
                        "    sys.executable,\n"
                        "    'scripts/run_report.py',\n"
                        "    '--topic', 'Workspace Mirror',\n"
                        "    '--notes', 'relative script ok',\n"
                        "], check=True)\n"
                    ),
                    "description": "Generated entrypoint that invokes a mirrored skill script.",
                }
            ],
            "cli_args": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    mirrored_script = Path(result["workspace"]) / "scripts" / "run_report.py"
    assert mirrored_script.is_file()
    report_path = Path(result["workspace"]) / "report.md"
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "# Workspace Mirror" in report_text
    assert "relative script ok" in report_text


@pytest.mark.asyncio
async def test_runtime_service_can_execute_multi_file_generated_bundle_via_entrypoint(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Run a generated multi-file helper bundle",
        user_query="write helper files first and then execute the generated entrypoint",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Run a generated multi-file helper bundle",
            "user_query": "write helper files first and then execute the generated entrypoint",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": None,
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "entrypoint": ".skill_generated/main.py",
            "rationale": "Execute the generated main entrypoint after writing helper files.",
            "generated_files": [
                {
                    "path": ".skill_generated/helpers.py",
                    "content": (
                        "from pathlib import Path\n"
                        "def write_output():\n"
                        "    Path('bundle.txt').write_text('bundle ok', encoding='utf-8')\n"
                        "    return 'bundle ok'\n"
                    ),
                    "description": "Helper module",
                },
                {
                    "path": ".skill_generated/main.py",
                    "content": (
                        "from helpers import write_output\n"
                        "print(write_output())\n"
                    ),
                    "description": "Generated entrypoint",
                },
            ],
            "cli_args": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["command_plan"]["entrypoint"] == ".skill_generated/main.py"
    assert (Path(result["workspace"]) / "bundle.txt").read_text(encoding="utf-8") == "bundle ok"


@pytest.mark.asyncio
async def test_runtime_service_compacts_large_generated_script_payload_for_transport(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    large_content = "print('transport-safe preview')\n" + ("# filler\n" * 7000)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Return a large generated script plan without overflowing transport",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell", "dry_run": True},
        command_plan={
            "skill_name": "pdf",
            "goal": "Return a large generated script plan without overflowing transport",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python ./.skill_generated/large.py",
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "rationale": "Exercise transport compaction for large generated files.",
            "generated_files": [
                {
                    "path": ".skill_generated/large.py",
                    "content": large_content,
                    "description": "Large helper script payload",
                }
            ],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    generated_entry = result["command_plan"]["generated_files"][0]
    assert result["run_status"] == "planned"
    assert generated_entry["path"] == ".skill_generated/large.py"
    assert generated_entry["content_truncated"] is True
    assert generated_entry["content_bytes"] > len(generated_entry["content"].encode("utf-8"))
    assert generated_entry["content"].startswith("print('transport-safe preview')")
    assert result["command_plan"]["hints"]["generated_files_transport_compacted"] is True

    status = module.get_run_status(result["run_id"])
    status_generated_entry = status["command_plan"]["generated_files"][0]
    assert status_generated_entry["content_truncated"] is True
    assert status_generated_entry["content_bytes"] == generated_entry["content_bytes"]


@pytest.mark.asyncio
async def test_runtime_service_starts_background_run_and_reports_running_status(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Quarterly Outlook",
        user_query="Include macro context",
        constraints={},
    )

    assert result["success"] is True
    assert result["run_status"] in {"running", "completed"}
    assert result["status"] in {"running", "completed"}
    assert result["runtime"]["delivery"] == "durable_worker"
    assert result["runtime"]["log_streaming"] is False
    assert result["runtime"]["log_transport"] == "poll"
    if result["run_status"] == "running":
        assert result["finished_at"] is None
        assert result["logs"]["stdout"] == ""
        assert result["artifacts"] == []

    status = await _wait_for_terminal_run(module, result["run_id"])
    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])

    assert status["run_status"] == "completed"
    assert logs["run_status"] == "completed"
    assert artifacts["run_status"] == "completed"
    assert logs["runtime"]["log_transport"] == "poll"
    assert logs["stdout"]
    assert artifacts["artifacts"][0]["path"] == "report.md"


@pytest.mark.asyncio
async def test_runtime_service_persists_run_state_outside_in_memory_cache(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Persist runtime state durably",
        user_query="make state survive cache clears",
        constraints={},
    )

    assert result["run_status"] in {"running", "completed"}
    module.RUN_STORE.clear()

    live_status = module.get_run_status(result["run_id"])
    assert live_status["run_status"] in {"running", "completed"}
    assert live_status["runtime"]["store_backend"] == "sqlite"

    final_status = await _wait_for_terminal_run(module, result["run_id"])
    module.RUN_STORE.clear()
    final_logs = module.get_run_logs(result["run_id"])

    assert final_status["run_status"] == "completed"
    assert final_logs["run_status"] == "completed"
    assert final_logs["runtime"]["store_backend"] == "sqlite"


def test_runtime_service_respects_queue_worker_concurrency(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("MCP_QUEUE_WORKER_CONCURRENCY", "3")
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    class _FakeProcess:
        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

    spawned = [3101, 3102, 3103]

    def _fake_spawn():
        return _FakeProcess(spawned.pop(0))

    module.QUEUE_WORKER_PROCESSES.clear()
    monkeypatch.setattr(module, "_spawn_queue_worker_process", _fake_spawn)

    worker_pids = module._ensure_queue_worker_processes()

    assert worker_pids == [3101, 3102, 3103]
    assert sorted(module.QUEUE_WORKER_PROCESSES) == [3101, 3102, 3103]


def _insert_stale_runtime_row(
    module,
    *,
    run_id: str,
    queue_state: str,
    attempt_count: int,
    max_attempts: int,
):
    record = {
        "run_id": run_id,
        "skill_name": "example-skill",
        "run_status": "running",
        "status": "running",
        "success": True,
        "summary": "stale worker test",
        "command": "python -c \"print('x')\"",
        "shell_mode": "conservative",
        "runtime_target": {
            "platform": "linux",
            "shell": "/bin/sh",
            "workspace_root": "/workspace",
            "workdir": "/workspace",
            "network_allowed": False,
            "supports_python": True,
        },
        "workspace": str((Path(module.WORKSPACE_ROOT) / run_id).resolve()),
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "failure_reason": None,
        "command_plan": {
            "skill_name": "example-skill",
            "goal": "stale worker",
            "user_query": "stale worker",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": "python -c \"print('x')\"",
            "mode": "explicit",
            "shell_mode": "conservative",
            "rationale": "stale worker test",
            "entrypoint": None,
            "cli_args": [],
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
        "runtime": {
            "executor": "shell",
            "delivery": "durable_worker",
            "queue_state": queue_state,
            "store_backend": "sqlite",
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
        },
        "execution_mode": "shell",
        "preflight": {"ok": True, "status": "ok", "failure_reason": None},
        "repair_attempted": False,
        "repair_succeeded": False,
        "repaired_from_run_id": None,
        "cancel_requested": False,
        "logs": {"stdout": "", "stderr": ""},
        "artifacts": [],
    }
    module._upsert_run_row(
        run_id=run_id,
        record=record,
        job_context={"skill_name": "example-skill"},
        queue_state=queue_state,
        worker_pid=999998,
        active_process_pid=999999,
        cancel_requested=False,
        lease_owner="pid:999998",
        lease_expires_at="2020-01-01T00:00:00+00:00",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        heartbeat=True,
    )
    with module._run_store_connect() as conn:
        conn.execute(
            """
            UPDATE skill_runs
            SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                run_id,
            ),
        )


def test_runtime_service_requeues_worker_lost_run_before_max_attempts(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "_ensure_queue_worker_processes", lambda: [])
    run_id = f"skill-run-{uuid.uuid4().hex}"
    _insert_stale_runtime_row(
        module,
        run_id=run_id,
        queue_state="executing",
        attempt_count=1,
        max_attempts=2,
    )

    status = module.get_run_status(run_id)

    assert status["run_status"] == "running"
    assert status["failure_reason"] is None
    assert status["runtime"]["queue_state"] == "queued"
    assert status["runtime"]["attempt_count"] == 1
    assert status["runtime"]["max_attempts"] == 2


def test_runtime_service_marks_worker_lost_after_max_attempts(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "_ensure_queue_worker_processes", lambda: [])
    run_id = f"skill-run-{uuid.uuid4().hex}"
    _insert_stale_runtime_row(
        module,
        run_id=run_id,
        queue_state="executing",
        attempt_count=2,
        max_attempts=2,
    )

    status = module.get_run_status(run_id)

    assert status["run_status"] == "failed"
    assert status["failure_reason"] == "worker_lost"
    assert status["runtime"]["queue_state"] == "failed"
    assert status["runtime"]["attempt_count"] == 2
    assert status["runtime"]["max_attempts"] == 2


def test_runtime_service_recovers_terminal_snapshot_before_requeue(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "_ensure_queue_worker_processes", lambda: [])
    run_id = f"skill-run-{uuid.uuid4().hex}"
    _insert_stale_runtime_row(
        module,
        run_id=run_id,
        queue_state="claimed",
        attempt_count=1,
        max_attempts=2,
    )

    row = module._load_run_row(run_id)
    assert row is not None
    record = module._inflate_run_record(row)
    workspace_dir = Path(record["workspace"]).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "recovered.txt").write_text("ok", encoding="utf-8")
    terminal_record = dict(record)
    terminal_record.update(
        {
            "run_status": "completed",
            "status": "completed",
            "success": True,
            "summary": "Recovered terminal state from workspace snapshot.",
            "finished_at": "2026-01-01T00:01:00+00:00",
            "exit_code": 0,
            "failure_reason": None,
            "artifacts": [{"path": "recovered.txt", "size_bytes": 2}],
            "logs": {"stdout": "done\n", "stderr": ""},
            "logs_preview": {
                "stdout": "done\n",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            },
            "runtime": {
                **dict(record.get("runtime", {})),
                "queue_state": "completed",
            },
        }
    )
    module._write_terminal_snapshot(workspace_dir=workspace_dir, record=terminal_record)

    status = module.get_run_status(run_id)
    artifacts = module.get_run_artifacts(run_id)

    assert status["run_status"] == "completed"
    assert status["success"] is True
    assert status["runtime"]["queue_state"] == "completed"
    assert status["runtime"]["terminal_snapshot_recovered"] is True
    assert artifacts["run_status"] == "completed"
    assert any(item["path"] == "recovered.txt" for item in artifacts["artifacts"])


@pytest.mark.asyncio
async def test_runtime_service_streams_live_logs_and_artifacts_while_running(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Observe live runtime output",
        user_query="stream the command output while it runs",
        constraints={},
        command_plan={
            "skill_name": "example-skill",
            "goal": "Observe live runtime output",
            "user_query": "stream the command output while it runs",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": (
                "python -c \"from pathlib import Path; import time; "
                "Path('mid.txt').write_text('partial', encoding='utf-8'); "
                "print('start', flush=True); time.sleep(0.8); print('end', flush=True)\""
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "rationale": "Long-running explicit command for live polling.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
    )

    assert result["run_status"] == "running"

    live_logs = await _wait_for_log_text(module, result["run_id"], "start")
    live_artifacts = await _wait_for_artifact_path(module, result["run_id"], "mid.txt")
    live_status = module.get_run_status(result["run_id"])

    assert live_logs["run_status"] == "running"
    assert "start" in live_logs["stdout"]
    assert live_artifacts["run_status"] == "running"
    assert any(item["path"] == "mid.txt" for item in live_artifacts["artifacts"])
    assert live_status["run_status"] == "running"

    final_status = await _wait_for_terminal_run(module, result["run_id"])
    final_logs = module.get_run_logs(result["run_id"])

    assert final_status["run_status"] == "completed"
    assert "end" in final_logs["stdout"]


@pytest.mark.asyncio
async def test_runtime_service_can_cancel_background_run(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Cancel a long-running shell command",
        user_query="cancel this runtime run",
        constraints={},
        command_plan={
            "skill_name": "example-skill",
            "goal": "Cancel a long-running shell command",
            "user_query": "cancel this runtime run",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": (
                "python -c \"import time; print('start', flush=True); "
                "time.sleep(5); print('end', flush=True)\""
            ),
            "mode": "explicit",
            "shell_mode": "conservative",
            "rationale": "Long-running explicit command for cancellation.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {},
        },
    )

    assert result["run_status"] == "running"
    await _wait_for_log_text(module, result["run_id"], "start")

    cancelled = await module.cancel_skill_run(result["run_id"])
    status = module.get_run_status(result["run_id"])
    logs = module.get_run_logs(result["run_id"])

    assert cancelled["run_status"] == "failed"
    assert cancelled["failure_reason"] in {"cancelled", "process_failed"}
    assert cancelled["cancel_requested"] is True
    assert status["run_status"] == "failed"
    assert status["failure_reason"] in {"cancelled", "process_failed"}
    assert status["cancel_requested"] is True
    assert logs["run_status"] == "failed"
    assert logs["cancel_requested"] is True
    assert "start" in logs["stdout"]


@pytest.mark.asyncio
async def test_runtime_service_can_plan_free_shell_directly_with_llm_client(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    llm = _StubLLM(
        {
            "mode": "generated_script",
            "command": "python ./.skill_generated/echo.py",
            "generated_files": [
                {
                    "path": ".skill_generated/echo.py",
                    "content": "print('ok')\n",
                    "description": "helper",
                }
            ],
            "rationale": "Plan directly inside the runtime service.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", llm)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Plan free shell inside the runtime service",
        user_query="use free shell mode",
        constraints={"shell_mode": "free_shell", "dry_run": True},
    )

    assert result["success"] is True
    assert result["run_status"] == "planned"
    assert result["shell_mode"] == "free_shell"
    assert result["runtime_target"]["platform"] == "linux"
    assert result["runtime_target"]["shell"] == "/bin/sh"
    assert result["command_plan"]["mode"] == "generated_script"
    assert len(llm.calls) == 1
    assert '"platform": "linux"' in llm.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_runtime_service_preflight_fails_when_required_tools_are_missing(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Attempt a tool that is not installed",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        command_plan={
            "skill_name": "pdf",
            "goal": "Attempt a tool that is not installed",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "missing-tool --version",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Requires a missing tool.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["missing-tool"]},
        },
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["failure_reason"] == "missing_required_tools"
    assert result["preflight"]["ok"] is False
    assert result["preflight"]["failure_reason"] == "missing_required_tools"
    assert result["preflight"]["required_tools"][0]["available"] is False


def test_runtime_service_preflight_allows_bootstrap_pending_required_tools(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    workspace_dir = (tmp_path / "preflight-bootstrap-pending").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    command_plan = module.SkillCommandPlan.from_dict(
        {
            "skill_name": "pptx",
            "goal": "Generate a deck",
            "user_query": "生成“科幻小说起源发展”的PPT文件",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": True,
                "supports_python": True,
            },
            "constraints": {"output_format": "pptx"},
            "command": "python .skill_generated/create_presentation.py",
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "entrypoint": ".skill_generated/create_presentation.py",
            "generated_files": [
                {
                    "path": ".skill_generated/create_presentation.py",
                    "content": "print('ok')\n",
                    "description": "stub presentation generator",
                }
            ],
            "bootstrap_commands": ["npm install -g pptxgenjs"],
            "bootstrap_reason": "Install pptxgenjs before execution.",
            "hints": {"planner": "free_shell", "required_tools": ["node", "npm", "python"]},
        },
        skill_name="pptx",
        goal="Generate a deck",
        user_query="生成“科幻小说起源发展”的PPT文件",
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        constraints={"output_format": "pptx"},
    )

    preflight, written_paths = module._run_preflight(
        workspace_dir=workspace_dir,
        command_plan=command_plan,
        env={"PATH": str(Path(module.sys.executable).resolve().parent)},
    )

    assert preflight["ok"] is True
    assert preflight["failure_reason"] is None
    assert written_paths == [".skill_generated/create_presentation.py"]
    required_tools = {item["name"]: item for item in preflight["required_tools"]}
    assert required_tools["node"]["available"] is False
    assert required_tools["node"]["bootstrap_pending"] is True
    assert required_tools["npm"]["available"] is False
    assert required_tools["npm"]["bootstrap_pending"] is True
    assert required_tools["python"]["available"] is True
    assert required_tools["python"]["bootstrap_pending"] is False


@pytest.mark.asyncio
async def test_runtime_service_bootstraps_missing_tool_before_execution(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Bootstrap a local helper tool before execution",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Bootstrap a local helper tool before execution",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "tool-from-bootstrap",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Create a local helper tool first, then execute it.",
            "generated_files": [],
                "bootstrap_commands": [
                    (
                        "python -c \"from pathlib import Path; import os; "
                        "bootstrap_bin=Path(os.environ['SKILL_BOOTSTRAP_BIN']); "
                        "bootstrap_bin.mkdir(parents=True, exist_ok=True); "
                        "target=bootstrap_bin/'tool-from-bootstrap.cmd'; "
                        "target.write_text('@echo off\\r\\necho bootstrapped tool>bootstrap.txt\\r\\n', encoding='utf-8')\""
                    )
                ],
            "bootstrap_reason": "Provision a workspace-local command shim before execution.",
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["tool-from-bootstrap"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["bootstrap_attempted"] is True
    assert result["bootstrap_succeeded"] is True
    assert result["bootstrap_attempt_count"] == 1
    assert result["bootstrap_attempt_limit"] >= 1
    assert len(result["bootstrap_history"]) == 1
    assert result["bootstrap_history"][0]["success"] is True
    assert result["bootstrap_history"][0]["duration_s"] is not None
    assert result["runtime"]["bootstrap_duration_s"] is not None
    assert result["runtime"]["execution_duration_s"] is not None
    assert (Path(result["workspace"]) / "bootstrap.txt").read_text(encoding="utf-8").strip() == (
        "bootstrapped tool"
    )

    status = module.get_run_status(result["run_id"])
    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])
    assert status["bootstrap_attempt_count"] == 1
    assert logs["bootstrap_attempt_count"] == 1
    assert artifacts["bootstrap_attempt_count"] == 1
    assert any(item.get("path") == "bootstrap.txt" for item in artifacts["artifacts"])


@pytest.mark.asyncio
async def test_runtime_service_uses_shared_envs_bootstrap_root_for_free_shell(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Use a shared bootstrap root under envs for free-shell setup",
        user_query="free shell bootstrap should be reusable",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Use a shared bootstrap root under envs for free-shell setup",
            "user_query": "free shell bootstrap should be reusable",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": (
                "python -c \"from pathlib import Path; import os; "
                "root=Path(os.environ['SKILL_BOOTSTRAP_ROOT']); "
                "Path('bootstrap-root.txt').write_text(str(root), encoding='utf-8'); "
                "Path('bootstrap-shared.txt').write_text(os.environ['SKILL_BOOTSTRAP_SHARED'], encoding='utf-8'); "
                "Path('marker-seen.txt').write_text((root/'marker.txt').read_text(encoding='utf-8'), encoding='utf-8')\""
            ),
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Inspect the resolved bootstrap root.",
            "generated_files": [],
            "bootstrap_commands": [
                (
                    "python -c \"from pathlib import Path; import os; "
                    "root=Path(os.environ['SKILL_BOOTSTRAP_ROOT']); "
                    "root.mkdir(parents=True, exist_ok=True); "
                    "(root/'marker.txt').write_text('ok', encoding='utf-8')\""
                )
            ],
            "bootstrap_reason": "Prepare a shared bootstrap marker.",
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    run_workspace = Path(result["workspace"]).resolve()
    bootstrap_root = Path(
        (run_workspace / "bootstrap-root.txt").read_text(encoding="utf-8").strip()
    ).resolve()
    expected_envs_root = (tmp_path / "runtime-workspace" / "envs").resolve()

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["bootstrap_attempted"] is True
    assert (run_workspace / "bootstrap-shared.txt").read_text(encoding="utf-8").strip() == "true"
    assert (run_workspace / "marker-seen.txt").read_text(encoding="utf-8").strip() == "ok"
    assert expected_envs_root in bootstrap_root.parents
    assert "bootstrap" in bootstrap_root.parts
    assert run_workspace not in bootstrap_root.parents
    assert (bootstrap_root / "marker.txt").read_text(encoding="utf-8").strip() == "ok"


@pytest.mark.asyncio
async def test_runtime_service_auto_provisions_node_runtime_before_npm_bootstrap(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    provision_calls: list[dict] = []
    workspace_dir = (tmp_path / "node-bootstrap-workspace").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    async def _fake_ensure_bootstrap_node_runtime(self, *, env: dict[str, str], timeout_s: int):
        provision_calls.append({"timeout_s": timeout_s, "bootstrap_root": env.get("SKILL_BOOTSTRAP_ROOT")})
        bootstrap_bin = Path(env["SKILL_BOOTSTRAP_BIN"]).resolve()
        bootstrap_bin.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            (bootstrap_bin / "node.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
            (bootstrap_bin / "npm.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        else:
            for name in ("node", "npm"):
                target = bootstrap_bin / name
                target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                target.chmod(0o755)
        env["PATH"] = str(bootstrap_bin) + os.pathsep + str(env.get("PATH", ""))
        started_at = self.deps.utc_now()
        finished_at = self.deps.utc_now()
        return {
            "success": True,
            "stdout": "[bootstrap runtime] Installed Node.js runtime for npm bootstrap\n",
            "stderr": "",
            "failure_reason": None,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": 0.0,
        }

    monkeypatch.setattr(
        module._EXECUTION_MANAGER,
        "ensure_bootstrap_node_runtime",
        types.MethodType(_fake_ensure_bootstrap_node_runtime, module._EXECUTION_MANAGER),
    )
    bootstrap_root = (workspace_dir / "bootstrap-root").resolve()
    env = dict(os.environ)
    env.update(
        {
            "SKILL_BOOTSTRAP_ROOT": str(bootstrap_root),
            "SKILL_BOOTSTRAP_BIN": str((bootstrap_root / "bin").resolve()),
            "SKILL_NETWORK_ALLOWED": "true",
            "HOME": str(workspace_dir),
            "NPM_CONFIG_PREFIX": str(bootstrap_root),
            "npm_config_prefix": str(bootstrap_root),
        }
    )

    result = await module._execute_bootstrap_commands(
        commands=["npm install -g pptxgenjs"],
        workspace_dir=workspace_dir,
        env=env,
        timeout_s=60,
    )

    assert result["success"] is True
    assert provision_calls
    assert "[bootstrap runtime] Installed Node.js runtime" in result["logs_preview"]["stdout"]
    assert shutil.which("npm", path=env["PATH"]) is not None


def test_runtime_service_normalizes_bootstrap_pip_install_for_reuse(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    command = 'python -m pip install --target "$SKILL_BOOTSTRAP_SITE_PACKAGES" lxml'
    normalized = module._EXECUTION_MANAGER._normalize_bootstrap_command_for_reuse(command)

    assert "--upgrade" in normalized
    assert "--upgrade-strategy only-if-needed" in normalized


@pytest.mark.asyncio
async def test_runtime_service_uses_command_plan_runtime_target_for_bootstrap_network_policy(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Allow bootstrap when the resolved command plan runtime target enables network",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Allow bootstrap when the resolved command plan runtime target enables network",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": True,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": (
                "python -c \"from pathlib import Path; "
                "Path('network-bootstrap-ok.txt').write_text('ok', encoding='utf-8')\""
            ),
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Bootstrap command should honor the plan runtime target.",
            "generated_files": [],
            "bootstrap_commands": [
                "python -m pip install --help > pip-install-help.txt"
            ],
            "bootstrap_reason": "Exercise the network-policy gate with an install-shaped command.",
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["bootstrap_attempted"] is True
    assert result["bootstrap_succeeded"] is True
    assert result["bootstrap_attempt_count"] == 1
    assert result["runtime_target"]["network_allowed"] is True
    assert (Path(result["workspace"]) / "network-bootstrap-ok.txt").read_text(
        encoding="utf-8"
    ) == "ok"
    assert (Path(result["workspace"]) / "pip-install-help.txt").is_file()

    status = module.get_run_status(result["run_id"])
    assert status["run_status"] == "completed"
    assert status["runtime_target"]["network_allowed"] is True
    assert status["bootstrap_history"][0]["success"] is True


@pytest.mark.asyncio
async def test_runtime_service_preflight_checks_generated_python_syntax(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Run generated helper script with a syntax error",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        command_plan={
            "skill_name": "pdf",
            "goal": "Run generated helper script with a syntax error",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python ./.skill_generated/bad.py",
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "rationale": "Broken helper",
            "generated_files": [
                {
                    "path": ".skill_generated/bad.py",
                    "content": "def broken(:\n    pass\n",
                    "description": "syntax error",
                }
            ],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is False
    assert result["run_status"] == "manual_required"
    assert result["failure_reason"] == "generated_python_syntax_error"
    assert result["preflight"]["generated_files"][0]["python_compile"]["ok"] is False


@pytest.mark.asyncio
async def test_runtime_service_repairs_failed_free_shell_execution_successfully(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        {
            "mode": "free_shell",
            "command": (
                "python -c \"from pathlib import Path; "
                "Path('repaired.txt').write_text('ok', encoding='utf-8'); "
                "print('repaired')\""
            ),
            "generated_files": [],
            "rationale": "Use a simple python repair command.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Repair a failed free shell command",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Repair a failed free shell command",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python -c \"import sys; print('boom'); sys.exit(1)\"",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Fail first, then repair.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["repair_attempt_count"] == 1
    assert result["repair_attempt_limit"] >= 1
    assert len(result["repair_history"]) == 1
    assert result["repair_history"][0]["stage"] == "execution"
    assert result["repaired_from_run_id"].endswith(":attempt-1")
    assert (Path(result["workspace"]) / "repaired.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_runtime_service_refreshes_unavailable_repair_llm_for_execution_repair(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    refreshed_llm = _StubLLM(
        {
            "mode": "free_shell",
            "command": (
                "python -c \"from pathlib import Path; "
                "Path('refreshed-repair.txt').write_text('ok', encoding='utf-8'); "
                "print('refreshed repair')\""
            ),
            "generated_files": [],
            "rationale": "Use the refreshed client to repair the failed command.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", _UnavailableLLM())
    module._EXECUTION_MANAGER.deps.refresh_llm_client = lambda: refreshed_llm

    payload, loaded_skill, request = module._build_skill_runtime_context(
        "pdf",
        "Repair a failed command after refreshing the repair client",
        "free shell mode",
        None,
        {"shell_mode": "free_shell"},
    )
    command_plan = module.SkillCommandPlan.from_dict(
        {
            "skill_name": "pdf",
            "goal": "Repair a failed command after refreshing the repair client",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python -c \"import sys; print('boom'); sys.exit(1)\"",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Fail first, then refresh the repair client.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
        skill_name="pdf",
        goal="Repair a failed command after refreshing the repair client",
        user_query="free shell mode",
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        constraints={"shell_mode": "free_shell"},
    )
    workspace_dir = (tmp_path / "runtime-refresh-repair").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    module._materialize_skill_workspace_view(
        loaded_skill=loaded_skill,
        workspace_dir=workspace_dir,
    )
    request_file = module._write_skill_request(
        workspace_dir=workspace_dir,
        skill_payload=payload,
        goal=request.goal,
        user_query=request.user_query,
        constraints=request.constraints,
        workspace=request.workspace,
        runtime_target=command_plan.runtime_target.to_dict(),
        command_plan=command_plan.to_dict(),
    )
    context_file = module._write_skill_context(
        workspace_dir=workspace_dir,
        skill_payload=payload,
    )
    env, skill_env_spec = module._build_run_env(
        skill_dir=loaded_skill.skill.path.resolve(),
        workspace_dir=workspace_dir,
        goal=request.goal,
        user_query=request.user_query,
        constraints=request.constraints,
        runtime_target=request.runtime_target,
        request_file=request_file,
        context_file=context_file,
        loaded_skill=loaded_skill,
        command_plan=command_plan,
        materialize_skill_env=False,
    )
    preflight, generated_files = module._run_preflight(
        workspace_dir=workspace_dir,
        command_plan=command_plan,
        env=env,
    )
    shell_command = module._materialize_shell_command(
        loaded_skill=loaded_skill,
        command_plan=command_plan,
        workspace_dir=workspace_dir,
    )

    result = await module._EXECUTION_MANAGER.complete_shell_run(
        skill_payload=payload,
        loaded_skill=loaded_skill,
        request=request,
        command_plan=command_plan,
        run_id=f"skill-run-{uuid.uuid4().hex}",
        started_at=module._utc_now(),
        workspace_dir=workspace_dir,
        request_file=request_file,
        context_file=context_file,
        generated_files=generated_files,
        preflight=preflight,
        shell_command=shell_command,
        env=env,
        skill_env_spec=skill_env_spec,
        timeout_s=module.DEFAULT_RUN_TIMEOUT_S,
        cleanup_workspace=False,
        worker_started_at=module._utc_now(),
    )

    assert result["success"] is True
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["repair_attempt_count"] == 1
    assert result["repair_history"][0]["stage"] == "execution"
    assert module.UTILITY_LLM_CLIENT is refreshed_llm
    assert (workspace_dir / "refreshed-repair.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_runtime_service_marks_worker_starting_before_durable_execution(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    payload, loaded_skill, request = module._build_skill_runtime_context(
        "pdf",
        "Advance claimed runs into worker_starting before execution",
        "free shell mode",
        None,
        {"shell_mode": "free_shell"},
    )
    command_plan = module.SkillCommandPlan.from_dict(
        {
            "skill_name": "pdf",
            "goal": "Advance claimed runs into worker_starting before execution",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python -c \"print('ok')\"",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Simple no-op command.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
        skill_name="pdf",
        goal="Advance claimed runs into worker_starting before execution",
        user_query="free shell mode",
        runtime_target=module.DEFAULT_RUNTIME_TARGET,
        constraints={"shell_mode": "free_shell"},
    )
    workspace_dir = (tmp_path / "worker-starting-workspace").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    module._materialize_skill_workspace_view(
        loaded_skill=loaded_skill,
        workspace_dir=workspace_dir,
    )
    request_file = module._write_skill_request(
        workspace_dir=workspace_dir,
        skill_payload=payload,
        goal=request.goal,
        user_query=request.user_query,
        constraints=request.constraints,
        workspace=request.workspace,
        runtime_target=command_plan.runtime_target.to_dict(),
        command_plan=command_plan.to_dict(),
    )
    context_file = module._write_skill_context(
        workspace_dir=workspace_dir,
        skill_payload=payload,
    )
    preflight = {
        "status": "ok",
        "ok": True,
        "failure_reason": None,
        "required_tools": [],
        "generated_files": [],
    }
    run_id = f"skill-run-{uuid.uuid4().hex}"
    started_at = module._utc_now()
    running_record = module.SkillRunRecord(
        skill_name=request.skill_name,
        run_status="running",
        success=True,
        summary="Queued durable run for worker-starting test.",
        command_plan=command_plan,
        run_id=run_id,
        command=command_plan.command,
        workspace=str(workspace_dir),
        started_at=started_at,
        runtime=module._durable_runtime_metadata(queue_state="claimed"),
        preflight=preflight,
    )
    running_payload = module._build_run_record_payload(
        record=running_record,
        goal=request.goal,
        user_query=request.user_query,
        constraints=request.constraints,
        payload=payload,
        loaded_skill=loaded_skill,
    )
    running_payload["request_file"] = str(request_file)
    running_payload["context_file"] = str(context_file)
    running_payload["generated_files"] = []
    running_payload["logs"] = {"stdout": "", "stderr": ""}
    module._store_run_record(running_payload)
    job_context = module._EXECUTION_MANAGER.build_worker_job_context(
        request=request,
        command_plan=command_plan,
        run_id=run_id,
        started_at=started_at,
        workspace_dir=workspace_dir,
        request_file=request_file,
        context_file=context_file,
        generated_files=[],
        preflight=preflight,
        shell_command=command_plan.command or "",
        timeout_s=module.DEFAULT_RUN_TIMEOUT_S,
        cleanup_workspace=False,
    )
    module._upsert_run_row(
        run_id=run_id,
        record=running_payload,
        job_context=job_context,
        queue_state="claimed",
        worker_pid=None,
        active_process_pid=None,
        cancel_requested=False,
        heartbeat=False,
    )

    observed: dict[str, object] = {}
    original_complete_shell_run = module._EXECUTION_MANAGER.complete_shell_run

    async def _fake_complete_shell_run(self, **kwargs):
        row = module._load_run_row(run_id)
        record = module._load_run_record(run_id)
        observed["row_queue_state"] = str(row["queue_state"]) if row is not None else None
        observed["runtime_queue_state"] = (
            str(record.get("runtime", {}).get("queue_state"))
            if isinstance(record, dict)
            else None
        )
        return {
            "success": True,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "logs_preview": {"stdout": "", "stderr": ""},
            "artifacts": [],
            "duration_s": 0.0,
        }

    module._EXECUTION_MANAGER.complete_shell_run = types.MethodType(
        _fake_complete_shell_run,
        module._EXECUTION_MANAGER,
    )
    try:
        result = await module._run_durable_worker(run_id)
    finally:
        module._EXECUTION_MANAGER.complete_shell_run = original_complete_shell_run

    assert result == 0
    assert observed["row_queue_state"] == "worker_starting"
    assert observed["runtime_queue_state"] == "worker_starting"


@pytest.mark.asyncio
async def test_runtime_service_can_upgrade_failed_conservative_execution_to_free_shell(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": (
                        "from pathlib import Path\n"
                        "Path('auto-free-shell.txt').write_text('ok', encoding='utf-8')\n"
                        "print('auto free shell repaired')\n"
                    ),
                    "description": "Generated repair entrypoint.",
                }
            ],
            "rationale": "Upgrade the failed conservative plan into a generated free-shell repair.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Repair a failed conservative command",
        user_query="build the example workflow",
        constraints={},
        wait_for_completion=True,
        command_plan={
            "skill_name": "example-skill",
            "goal": "Repair a failed conservative command",
            "user_query": "build the example workflow",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": "python -c \"import sys; print('boom'); sys.exit(1)\"",
            "mode": "inferred",
            "shell_mode": "conservative",
            "rationale": "Fail first, then auto-upgrade.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "locked_shipped_script", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["repair_attempt_count"] == 1
    assert result["shell_mode"] == "free_shell"
    assert result["command_plan"]["shell_mode"] == "free_shell"
    assert result["repair_history"][0]["stage"] == "execution"
    assert (Path(result["workspace"]) / "auto-free-shell.txt").read_text(
        encoding="utf-8"
    ) == "ok"


@pytest.mark.asyncio
async def test_runtime_service_can_upgrade_skill_env_failure_to_free_shell(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": (
                        "from pathlib import Path\n"
                        "Path('env-repaired.txt').write_text('ok', encoding='utf-8')\n"
                        "print('env repaired')\n"
                    ),
                    "description": "Generated repair entrypoint.",
                }
            ],
            "rationale": "Upgrade the failed conservative env setup into a generated free-shell repair.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    original_build_run_env = module._EXECUTION_MANAGER.deps.build_run_env

    def _failing_build_run_env(**kwargs):
        command_plan = kwargs.get("command_plan")
        if (
            command_plan is not None
            and getattr(command_plan, "shell_mode", "") == "conservative"
        ):
            raise module.SkillServerError("simulated skill env failure")
        return original_build_run_env(**kwargs)

    module._EXECUTION_MANAGER.deps.build_run_env = _failing_build_run_env

    result = await module.run_skill_task(
        skill_name="example-skill",
        goal="Repair a conservative skill env failure",
        user_query="build the example workflow",
        constraints={},
        wait_for_completion=True,
        command_plan={
            "skill_name": "example-skill",
            "goal": "Repair a conservative skill env failure",
            "user_query": "build the example workflow",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {},
            "command": "python -c \"print('will not run')\"",
            "mode": "inferred",
            "shell_mode": "conservative",
            "rationale": "Env setup fails first, then auto-upgrade.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "locked_shipped_script", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["repair_attempt_count"] == 1
    assert result["shell_mode"] == "free_shell"
    assert result["command_plan"]["shell_mode"] == "free_shell"
    assert result["repair_history"][0]["stage"] in {"environment", "preflight"}
    assert (Path(result["workspace"]) / "env-repaired.txt").read_text(
        encoding="utf-8"
    ) == "ok"


@pytest.mark.asyncio
async def test_runtime_service_can_repair_failed_free_shell_preflight_before_execution(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": (
                        "from pathlib import Path\n"
                        "Path('preflight-repaired.txt').write_text('ok', encoding='utf-8')\n"
                        "print('preflight repaired')\n"
                    ),
                    "description": "Fixed entry script.",
                }
            ],
            "rationale": "Replace the invalid generated helper with a valid script bundle.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Repair a generated script before execution",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Repair a generated script before execution",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": None,
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "entrypoint": ".skill_generated/main.py",
            "rationale": "Fail preflight first, then repair.",
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": "def broken(:\n    pass\n",
                    "description": "Broken helper",
                }
            ],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["repair_attempted"] is True
    assert result["repair_attempt_count"] == 1
    assert result["repair_history"][0]["stage"] == "preflight"
    assert (Path(result["workspace"]) / "preflight-repaired.txt").read_text(
        encoding="utf-8"
    ) == "ok"


@pytest.mark.asyncio
async def test_runtime_service_can_complete_after_multiple_free_shell_repairs(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        [
            {
                "mode": "free_shell",
                "command": "python -c \"import sys; print('still bad'); sys.exit(2)\"",
                "generated_files": [],
                "rationale": "First repair still fails.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            },
            {
                "mode": "generated_script",
                "command": None,
                "entrypoint": ".skill_generated/main.py",
                "cli_args": [],
                "generated_files": [
                    {
                        "path": ".skill_generated/main.py",
                        "content": (
                            "from pathlib import Path\n"
                            "Path('final-repaired.txt').write_text('ok', encoding='utf-8')\n"
                            "print('final repair worked')\n"
                        ),
                        "description": "Working repair entrypoint.",
                    }
                ],
                "rationale": "Second repair writes a real helper script and succeeds.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            },
        ]
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Repair a failed free shell command more than once",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Repair a failed free shell command more than once",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python -c \"import sys; print('boom'); sys.exit(1)\"",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Fail first, fail once more, then repair.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is True
    assert result["run_status"] == "completed"
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is True
    assert result["repair_attempt_count"] == 2
    assert result["repair_attempt_limit"] >= 2
    assert [item["stage"] for item in result["repair_history"]] == [
        "execution",
        "execution",
    ]
    assert result["repair_history"][0]["snapshot_run_id"].endswith(":attempt-1")
    assert result["repair_history"][1]["snapshot_run_id"].endswith(":attempt-2")
    assert (Path(result["workspace"]) / "final-repaired.txt").read_text(
        encoding="utf-8"
    ) == "ok"

    status = module.get_run_status(result["run_id"])
    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])
    assert status["repair_attempt_count"] == 2
    assert logs["repair_attempt_count"] == 2
    assert artifacts["repair_attempt_count"] == 2
    assert len(status["repair_history"]) == 2


@pytest.mark.asyncio
async def test_runtime_service_reports_failed_repair_attempt(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    repair_llm = _StubLLM(
        {
            "mode": "free_shell",
            "command": "python -c \"import sys; print('still bad'); sys.exit(2)\"",
            "generated_files": [],
            "rationale": "Bad repair",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    monkeypatch.setattr(module, "UTILITY_LLM_CLIENT", repair_llm)

    result = await module.run_skill_task(
        skill_name="pdf",
        goal="Repair fails as well",
        user_query="free shell mode",
        constraints={"shell_mode": "free_shell"},
        wait_for_completion=True,
        command_plan={
            "skill_name": "pdf",
            "goal": "Repair fails as well",
            "user_query": "free shell mode",
            "runtime_target": {
                "platform": "linux",
                "shell": "/bin/sh",
                "workspace_root": "/workspace",
                "workdir": "/workspace",
                "network_allowed": False,
                "supports_python": True,
            },
            "constraints": {"shell_mode": "free_shell"},
            "command": "python -c \"import sys; print('boom'); sys.exit(1)\"",
            "mode": "free_shell",
            "shell_mode": "free_shell",
            "rationale": "Fail first, fail again.",
            "generated_files": [],
            "missing_fields": [],
            "failure_reason": None,
            "hints": {"planner": "free_shell", "required_tools": ["python"]},
        },
    )

    assert result["success"] is False
    assert result["run_status"] == "failed"
    assert result["repair_attempted"] is True
    assert result["repair_succeeded"] is False
    assert result["repair_attempt_count"] >= 1
    assert len(result["repair_history"]) >= 1
    assert result["repaired_from_run_id"].endswith(":attempt-1")
