from pathlib import Path

import pytest

from kg_agent.skills import (
    SkillCommandPlanner,
    SkillExecutionRequest,
    SkillExecutor,
    SkillLoader,
    SkillRegistry,
    SkillRuntimeTarget,
)


class _StubPlannerLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def is_available(self) -> bool:
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.payload)


def _load_repo_skill(skill_name: str):
    registry = SkillRegistry(Path("skills"))
    loader = SkillLoader(registry)
    return loader.load_skill(skill_name)


@pytest.mark.asyncio
async def test_skill_command_planner_uses_explicit_shell_command():
    loaded_skill = _load_repo_skill("example-skill")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="example-skill",
            goal="Build the report",
            user_query="run the example skill",
            constraints={
                "shell_command": "python scripts/run_report.py --topic 'Explicit'",
            },
        ),
    )

    assert plan.mode == "explicit"
    assert plan.command == "python scripts/run_report.py --topic 'Explicit'"


@pytest.mark.asyncio
async def test_skill_command_planner_builds_command_from_structured_args():
    loaded_skill = _load_repo_skill("example-skill")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="example-skill",
            goal="Ignored by args",
            user_query="create the report",
            constraints={
                "args": {
                    "topic": "Board Update",
                    "notes": "Use the structured args path.",
                }
            },
        ),
    )

    assert plan.mode == "structured_args"
    assert plan.entrypoint == "scripts/run_report.py"
    assert "--topic" in plan.cli_args
    assert "Board Update" in plan.command


@pytest.mark.asyncio
async def test_skill_command_planner_infers_single_entrypoint_command():
    loaded_skill = _load_repo_skill("example-skill")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="example-skill",
            goal="Quarterly Outlook",
            user_query="Include macro context",
            constraints={},
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/run_report.py"
    assert plan.cli_args[:2] == ["--topic", "Quarterly Outlook"]
    assert "--notes" in plan.cli_args
    assert plan.runtime_target.platform == "linux"
    assert plan.runtime_target.shell == "/bin/sh"


@pytest.mark.asyncio
async def test_skill_command_planner_supports_xlsx_recalc():
    loaded_skill = _load_repo_skill("xlsx")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="xlsx",
            goal="Recalculate spreadsheet formulas",
            user_query='recalculate formulas in "C:\\Reports\\model.xlsx"',
            constraints={
                "operation": "recalc",
                "input_path": "C:\\Reports\\model.xlsx",
            },
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/recalc.py"
    assert plan.cli_args == ["C:\\Reports\\model.xlsx"]


@pytest.mark.asyncio
async def test_skill_command_planner_returns_manual_required_for_ambiguous_target():
    loaded_skill = _load_repo_skill("example-skill")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="example-skill",
            goal="Create one report",
            user_query="choose the right workbook",
            constraints={
                "input_paths": ["a.xlsx", "b.xlsx"],
            },
        ),
    )

    assert plan.mode == "manual_required"
    assert plan.failure_reason == "ambiguous_target_file"
    assert plan.missing_fields == ["input_path"]


@pytest.mark.asyncio
async def test_skill_command_planner_returns_manual_required_for_missing_credentials(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skills" / "credential-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "# credential-skill\n\n"
        "Use this skill when an API-backed workflow is required.\n\n"
        "```bash\npython scripts/run.py --api-key $API_KEY --input data.json\n```\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path / "skills")
    loader = SkillLoader(registry)
    loaded_skill = loader.load_skill("credential-skill")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="credential-skill",
            goal="Run the API workflow",
            user_query="execute it",
            constraints={},
        ),
    )

    assert plan.mode == "manual_required"
    assert plan.failure_reason == "missing_credential"
    assert "api_key" in plan.missing_fields


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_can_generate_complex_command():
    loaded_skill = _load_repo_skill("pdf")
    planner = SkillCommandPlanner(
        llm_client=_StubPlannerLLM(
            {
                "mode": "free_shell",
                "command": "qpdf --empty --pages input1.pdf input2.pdf -- merged.pdf && scp merged.pdf user@host:/tmp/",
                "generated_files": [],
                "rationale": "The task needs a chained shell workflow.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["qpdf", "scp"],
                "warnings": ["Requires qpdf and scp on PATH."],
            }
        )
    )

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="pdf",
            goal="Merge two PDFs then upload the result",
            user_query="use free shell mode to merge two pdfs and scp the output",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.shell_mode == "free_shell"
    assert plan.mode == "free_shell"
    assert "scp" in (plan.command or "")
    assert plan.hints["planner"] == "free_shell"
    assert plan.runtime_target.platform == "linux"


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_can_generate_script_from_python_examples():
    loaded_skill = _load_repo_skill("pdf")
    planner = SkillCommandPlanner(
        llm_client=_StubPlannerLLM(
            {
                "mode": "generated_script",
                "command": "python ./.skill_generated/extract_text.py",
                "generated_files": [
                    {
                        "path": ".skill_generated/extract_text.py",
                        "content": "from pypdf import PdfReader\nprint('ok')\n",
                        "description": "Temporary extraction helper generated from PDF examples.",
                    }
                ],
                "rationale": "Converted the PDF Python example into a runnable helper script.",
                "missing_fields": [],
                "failure_reason": None,
                "required_tools": ["python"],
                "warnings": [],
            }
        )
    )

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="pdf",
            goal="Extract text from a pdf with free shell mode",
            user_query="free shell mode: use the pdf examples to write a helper script and run it",
            constraints={"shell_mode": "free_shell", "input_path": "sample.pdf"},
        ),
    )

    assert plan.mode == "generated_script"
    assert plan.generated_files[0].path == ".skill_generated/extract_text.py"
    assert "pypdf" in plan.generated_files[0].content
    assert plan.command == "python ./.skill_generated/extract_text.py"


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_prompt_uses_linux_runtime_target():
    loaded_skill = _load_repo_skill("pdf")
    llm = _StubPlannerLLM(
        {
            "mode": "free_shell",
            "command": "python tools/run.py",
            "generated_files": [],
            "rationale": "Use the linux target runtime.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    planner = SkillCommandPlanner(
        llm_client=llm,
        default_runtime_target=SkillRuntimeTarget.linux_default(),
    )

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="pdf",
            goal="Run the PDF workflow with free shell mode",
            user_query="free shell mode on the runtime target",
            shell_mode="free_shell",
            runtime_target=SkillRuntimeTarget.linux_default(),
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.runtime_target.platform == "linux"
    assert plan.runtime_target.shell == "/bin/sh"
    assert len(llm.calls) == 1
    assert '"platform": "linux"' in llm.calls[0]["user_prompt"]
    assert '"shell": "/bin/sh"' in llm.calls[0]["user_prompt"]
    assert '"workspace_root": "/workspace"' in llm.calls[0]["user_prompt"]


@pytest.mark.asyncio
async def test_skill_executor_runs_through_command_planner_without_runtime():
    registry = SkillRegistry(Path("skills"))
    executor = SkillExecutor(registry=registry, loader=SkillLoader(registry))

    result = await executor.execute(
        skill_name="example-skill",
        goal="Quarterly Outlook",
        user_query="Include macro context",
        constraints={},
    )

    assert result.success is True
    assert result.data["run_status"] == "planned"
    assert result.data["status"] == "planned"
    assert result.data["command_plan"]["mode"] == "inferred"


@pytest.mark.asyncio
async def test_skill_executor_returns_manual_required_without_runtime():
    registry = SkillRegistry(Path("skills"))
    executor = SkillExecutor(registry=registry, loader=SkillLoader(registry))

    result = await executor.execute(
        skill_name="xlsx",
        goal="Fix the spreadsheet",
        user_query="repair this workbook",
        constraints={},
    )

    assert result.success is False
    assert result.data["run_status"] == "manual_required"
    assert result.data["status"] == "needs_shell_command"
    assert result.data["command_plan"]["mode"] == "manual_required"
