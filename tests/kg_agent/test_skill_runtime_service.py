from __future__ import annotations

import asyncio
import importlib.util
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
    assert result["run_status"] == "running"
    assert result["status"] == "running"
    assert result["finished_at"] is None
    assert result["runtime"]["delivery"] == "durable_worker"
    assert result["logs"]["stdout"] == ""
    assert result["artifacts"] == []

    status = await _wait_for_terminal_run(module, result["run_id"])
    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])

    assert status["run_status"] == "completed"
    assert logs["run_status"] == "completed"
    assert artifacts["run_status"] == "completed"
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

    assert result["run_status"] == "running"
    module.RUN_STORE.clear()

    live_status = module.get_run_status(result["run_id"])
    assert live_status["run_status"] == "running"
    assert live_status["runtime"]["store_backend"] == "sqlite"

    final_status = await _wait_for_terminal_run(module, result["run_id"])
    module.RUN_STORE.clear()
    final_logs = module.get_run_logs(result["run_id"])

    assert final_status["run_status"] == "completed"
    assert final_logs["run_status"] == "completed"
    assert final_logs["runtime"]["store_backend"] == "sqlite"


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
    assert cancelled["failure_reason"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert status["run_status"] == "failed"
    assert status["failure_reason"] == "cancelled"
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
async def test_runtime_service_preflight_checks_generated_python_syntax(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)

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
    assert result["repaired_from_run_id"].endswith(":attempt-1")
    assert (Path(result["workspace"]) / "repaired.txt").read_text(encoding="utf-8") == "ok"


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
    assert result["repaired_from_run_id"].endswith(":attempt-1")
