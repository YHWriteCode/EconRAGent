from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest


def _load_runtime_service_module(*, tmp_path: Path, monkeypatch) -> object:
    skills_root = Path("skills").resolve()
    workspace_root = (tmp_path / "runtime-workspace").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("MCP_WORKSPACE_DIR", str(workspace_root))
    module_path = Path("mcp-server/server.py").resolve()
    module_name = f"skill_runtime_service_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load skill runtime service module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    )

    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["execution_mode"] == "shell"
    assert "run_report.py" in result["command"]
    assert result["shell_plan"]["mode"] == "single_runnable_script_inferred_required_args"
    assert result["artifacts"][0]["path"] == "report.md"

    report_path = Path(result["workspace"]) / "report.md"
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "# Quarterly Outlook" in report_text
    assert "Include macro context" in report_text

    logs = module.get_run_logs(result["run_id"])
    artifacts = module.get_run_artifacts(result["run_id"])
    assert logs["stdout"]
    assert artifacts["artifacts"][0]["path"] == "report.md"


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
    assert result["status"] == "needs_shell_command"
    assert result["execution_mode"] == "shell"
    assert result["shell_plan"]["mode"] == "needs_shell_command"
    assert result["shell_plan"]["runnable_scripts"]


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
    )

    assert result["success"] is True
    assert result["shell_plan"]["mode"] == "single_runnable_script_with_structured_args"
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
    assert result["status"] == "planned"
    assert result["execution_mode"] == "shell"
    assert result["shell_plan"]["mode"] == "xlsx_recalc_from_constraints"
    assert "scripts/recalc.py" in result["command"].replace("\\", "/")
    assert "model.xlsx" in result["command"]
