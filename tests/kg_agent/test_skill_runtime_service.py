from __future__ import annotations

import asyncio
import importlib.util
import os
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


class _UnavailableLLM:
    def is_available(self) -> bool:
        return False


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
    assert result["command_plan"]["hints"]["runnable_scripts"]

    status = module.get_run_status(result["run_id"])
    assert status["run_status"] == "manual_required"
    assert status["status"] == "needs_shell_command"
    assert status["failure_reason"] == result["failure_reason"]


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
