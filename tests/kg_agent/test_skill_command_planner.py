from pathlib import Path

import pytest

import kg_agent.skills.command_planner as command_planner_module
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


class _FixedDate:
    @staticmethod
    def today():
        from datetime import date

        return date(2026, 4, 15)


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


def test_extract_relative_date_windows_supports_recent_year_and_three_months(monkeypatch):
    monkeypatch.setattr(command_planner_module, "_current_date", _FixedDate.today)

    windows = command_planner_module.extract_relative_date_windows(
        "请分析最近一年比亚迪股票的波动情况，并判断近3个月是否上涨"
    )

    assert len(windows) == 2
    primary = command_planner_module._select_primary_relative_date_window(windows)
    secondary = command_planner_module._select_secondary_relative_date_window(windows)
    assert primary is not None
    assert secondary is not None
    assert primary.start.strftime("%Y%m%d") == "20250415"
    assert primary.end.strftime("%Y%m%d") == "20260415"
    assert secondary.start.strftime("%Y%m%d") == "20260115"
    assert secondary.end.strftime("%Y%m%d") == "20260415"


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
async def test_skill_command_planner_conservative_mode_can_lock_clear_financial_script(
    monkeypatch,
):
    monkeypatch.setattr(command_planner_module, "_current_date", _FixedDate.today)
    loaded_skill = _load_repo_skill("financial-researching")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="获取比亚迪股票（002594）最近一年的价格波动数据，并分析其最近三个月内是否存在上涨趋势。",
            user_query="请帮我查找以下最近一年比亚迪002594股票的波动情况，3个月内是否有上涨趋势",
            constraints={},
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/analyze_stock_trend.py"
    assert plan.shell_mode == "conservative"
    assert "--code" in plan.cli_args
    assert "002594" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20250415" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260415" in plan.cli_args
    assert "--trend-start" in plan.cli_args
    assert "20260115" in plan.cli_args
    assert "--trend-end" in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_llm_constraint_inference_handles_fuzzy_dates():
    loaded_skill = _load_repo_skill("financial-researching")
    llm = _StubPlannerLLM(
        {
            "constraints": {
                "code": "002594",
                "start": "20250416",
                "end": "20260416",
                "trend_start": "20260116",
                "trend_end": "20260416",
            },
            "reason": "Normalized fuzzy dates relative to the provided reference date.",
            "confidence": "high",
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="分析比亚迪 002594 从去年同期到现在的波动情况，并判断最近一个季度是否还在上行",
            user_query="请看一下比亚迪002594从去年同期到现在的波动情况，并判断最近一个季度是否还在上行",
            constraints={},
        ),
    )

    assert len(llm.calls) == 1
    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/analyze_stock_trend.py"
    assert "--code" in plan.cli_args
    assert "002594" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20250416" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260416" in plan.cli_args
    assert "--trend-start" in plan.cli_args
    assert "20260116" in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_can_infer_tsla_trend_command_from_route_constraints(
    monkeypatch,
):
    monkeypatch.setattr(command_planner_module, "_current_date", _FixedDate.today)
    loaded_skill = _load_repo_skill("financial-researching")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="Use skill 'financial-researching' to fulfill the user request: 帮我查一下最近1个月，Tesla股价波动情况",
            user_query="帮我查一下最近1个月，Tesla股价波动情况",
            constraints={
                "timeframe": "最近1个月 (past month)",
                "target": "Tesla (TSLA) stock price",
                "analysis_type": "波动情况 (volatility/fluctuation analysis)",
            },
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/analyze_stock_trend.py"
    assert plan.shell_mode == "conservative"
    assert "--code" in plan.cli_args
    assert "TSLA" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20260315" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260415" in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_prefers_fetch_market_data_for_simple_price_query():
    loaded_skill = _load_repo_skill("financial-researching")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="获取宁德时代（300750）昨天的股价数据",
            user_query="请帮我查一下宁德时代300750昨天的股价",
            constraints={
                "code": "300750",
                "start": "20260414",
                "end": "20260414",
                "data_type": "stock price",
            },
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/fetch_market_data.py"
    assert "--codes" in plan.cli_args
    assert "300750" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20260414" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260414" in plan.cli_args
    assert "--output" not in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_ignores_llm_inferred_output_path_for_simple_financial_fetch():
    loaded_skill = _load_repo_skill("financial-researching")
    llm = _StubPlannerLLM(
        {
            "constraints": {
                "code": "300750",
                "start": "20260414",
                "end": "20260414",
                "output_path": "/workspace/output/market_data.csv",
            },
            "reason": "Normalized the request into shipped script arguments.",
            "confidence": "high",
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="获取宁德时代（300750）昨天的股价数据",
            user_query="请帮我查一下宁德时代300750昨天的股价",
            constraints={},
        ),
    )

    assert len(llm.calls) == 1
    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/fetch_market_data.py"
    assert "--output" not in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_infers_yesterday_fetch_dates_without_explicit_constraints(
    monkeypatch,
):
    monkeypatch.setattr(command_planner_module, "_current_date", _FixedDate.today)
    loaded_skill = _load_repo_skill("financial-researching")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="获取宁德时代（300750）昨天的股价数据",
            user_query="请帮我查一下宁德时代300750昨天的股价",
            constraints={},
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/fetch_market_data.py"
    assert "--start" in plan.cli_args
    assert "20260414" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260414" in plan.cli_args


@pytest.mark.asyncio
async def test_skill_command_planner_infers_realtime_tesla_trend_dates_when_missing(
    monkeypatch,
):
    monkeypatch.setattr(command_planner_module, "_current_date", _FixedDate.today)
    loaded_skill = _load_repo_skill("financial-researching")
    planner = SkillCommandPlanner()

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="现在特斯拉 TSLA 的股价是多少？顺便看下最近走势",
            user_query="现在特斯拉 TSLA 的股价是多少？顺便看下最近走势",
            constraints={},
        ),
    )

    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/analyze_stock_trend.py"
    assert "--code" in plan.cli_args
    assert "TSLA" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20260115" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260415" in plan.cli_args
    assert "--trend-start" in plan.cli_args
    assert "20260316" in plan.cli_args
    assert "--trend-end" in plan.cli_args
    assert "20260415" in plan.cli_args


def test_infer_script_cli_args_normalizes_directory_output_scripts():
    script_path = Path("skills/financial-researching/scripts/fetch_model_backtest.py")

    cli_args, missing_fields, inferred_flag_count = command_planner_module.infer_script_cli_args(
        script_path=script_path,
        goal="抓取宁德时代(300750)并执行端到端回测",
        user_query="请完成宁德时代300750的端到端建模回测",
        constraints={
            "codes": "000001,300750",
            "target": "300750",
            "start": "20260401",
            "end": "20260415",
            "output_path": "/workspace/output/market_data.csv",
        },
    )

    assert missing_fields == []
    assert inferred_flag_count >= 5
    assert "--output" in cli_args
    output_index = cli_args.index("--output")
    assert cli_args[output_index + 1] == "/workspace/output"


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
async def test_skill_command_planner_free_shell_promotes_inline_python_into_generated_script(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skills" / "inline-python-skill"
    skill_dir.mkdir(parents=True)
    skill_md = ["# inline-python-skill", "", "Use rich Python examples to assemble the workflow.", ""]
    for index in range(1, 5):
        skill_md.extend(
            [
                f"## Example {index}",
                "```python",
                f"print('sample-{index}')",
                "```",
                "",
            ]
        )
    (skill_dir / "SKILL.md").write_text("\n".join(skill_md), encoding="utf-8")

    registry = SkillRegistry(tmp_path / "skills")
    loader = SkillLoader(registry)
    loaded_skill = loader.load_skill("inline-python-skill")
    planner = SkillCommandPlanner(
        llm_client=_StubPlannerLLM(
            {
                "mode": "free_shell",
                "command": (
                    "python -c \"from pathlib import Path; "
                    "Path('auto.txt').write_text('ok', encoding='utf-8'); "
                    "print('auto')\""
                ),
                "generated_files": [],
                "rationale": "Inline Python is enough for the raw shell plan.",
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
            skill_name="inline-python-skill",
            goal="Turn the inline Python into a script-first workflow",
            user_query="write a helper script first and then execute it",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.mode == "generated_script"
    assert plan.entrypoint == ".skill_generated/main.py"
    assert plan.command == "python ./.skill_generated/main.py"
    assert "auto.txt" in plan.generated_files[0].content
    assert plan.hints["promoted_inline_python_to_generated_script"] is True


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_preserves_bootstrap_commands():
    loaded_skill = _load_repo_skill("pdf")
    planner = SkillCommandPlanner(
        llm_client=_StubPlannerLLM(
            {
                "mode": "free_shell",
                "command": "python ./.skill_generated/main.py",
                "entrypoint": ".skill_generated/main.py",
                "cli_args": [],
                "generated_files": [
                    {
                        "path": ".skill_generated/main.py",
                        "content": "print('ok')\n",
                        "description": "Entry script.",
                    }
                ],
                "bootstrap_commands": [
                    "python -m pip install --target ./.skill_bootstrap/site-packages pypdf"
                ],
                "bootstrap_reason": "Install the PDF dependency into the workspace bootstrap area.",
                "rationale": "Bootstrap the dependency and then run the generated script.",
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
            goal="Bootstrap a PDF dependency before execution",
            user_query="use free shell mode and install the dependency first",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.bootstrap_commands == [
        "python -m pip install --target ./.skill_bootstrap/site-packages pypdf"
    ]
    assert plan.bootstrap_reason == "Install the PDF dependency into the workspace bootstrap area."


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_prefers_shipped_single_entrypoint_over_generated_helper():
    loaded_skill = _load_repo_skill("example-skill")
    llm = _StubPlannerLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": "print('free-shell report')\n",
                    "description": "Generated report helper.",
                }
            ],
            "rationale": "Use a generated helper instead of the conservative single-script path.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="example-skill",
            goal="Prepare a custom report workflow",
            user_query="write a helper script and then run it",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert len(llm.calls) == 0
    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/run_report.py"
    assert plan.hints["planner"] == "locked_shipped_script"
    assert plan.hints["shipped_script_locked"] is True


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_can_pick_main_entrypoint_from_multi_file_bundle():
    loaded_skill = _load_repo_skill("pdf")
    planner = SkillCommandPlanner(
        llm_client=_StubPlannerLLM(
            {
                "mode": "generated_script",
                "command": None,
                "cli_args": ["sample.pdf"],
                "generated_files": [
                    {
                        "path": ".skill_generated/helpers.py",
                        "content": "def run(path):\n    return path\n",
                        "description": "Helper module.",
                    },
                    {
                        "path": ".skill_generated/main.py",
                        "content": (
                            "from helpers import run\n"
                            "print(run('sample.pdf'))\n"
                        ),
                        "description": "Entry script.",
                    },
                ],
                "rationale": "Synthesize a multi-file helper bundle from the PDF examples.",
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
            goal="Build a helper bundle from many examples",
            user_query="write helper modules from the pdf examples and run the main script",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.mode == "generated_script"
    assert plan.entrypoint == ".skill_generated/main.py"
    assert plan.cli_args == ["sample.pdf"]
    assert plan.command == "python ./.skill_generated/main.py sample.pdf"


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_prompt_includes_many_python_examples_and_fallback(
    tmp_path: Path,
):
    skill_dir = tmp_path / "skills" / "doc-heavy"
    skill_dir.mkdir(parents=True)
    skill_md = ["# doc-heavy", "", "Use many Python snippets to assemble a workflow.", ""]
    for index in range(1, 7):
        skill_md.extend(
            [
                f"## Example {index}",
                "```python",
                f"print('example-{index}')",
                "```",
                "",
            ]
        )
    (skill_dir / "SKILL.md").write_text("\n".join(skill_md), encoding="utf-8")

    registry = SkillRegistry(tmp_path / "skills")
    loader = SkillLoader(registry)
    loaded_skill = loader.load_skill("doc-heavy")
    llm = _StubPlannerLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": "print('ok')\n",
                    "description": "Entry script",
                }
            ],
            "rationale": "Use the example-heavy docs.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="doc-heavy",
            goal="Assemble a helper script from the examples",
            user_query="write a helper script from the examples",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert plan.mode == "generated_script"
    assert len(llm.calls) == 1
    assert "Conservative fallback plan:" in llm.calls[0]["user_prompt"]
    assert "print('example-6')" in llm.calls[0]["user_prompt"]


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
async def test_skill_command_planner_free_shell_prefers_shipped_script_for_clear_multi_script_match():
    loaded_skill = _load_repo_skill("financial-researching")
    llm = _StubPlannerLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": "print('should not be used')\n",
                    "description": "Unwanted helper",
                }
            ],
            "rationale": "Generate a fresh helper anyway.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="抓取宁德时代(300750)从 2023-01-03 到 2026-04-15 的历史股票数据，并完成端到端建模与回测",
            user_query="free shell 模式下直接用现有脚本完成，不要另写 helper script",
            constraints={
                "shell_mode": "free_shell",
                "output_path": "output",
            },
        ),
    )

    assert len(llm.calls) == 0
    assert plan.mode == "inferred"
    assert plan.entrypoint == "scripts/fetch_model_backtest.py"
    assert plan.shell_mode == "free_shell"
    assert "--target" in plan.cli_args
    assert "300750" in plan.cli_args
    assert "--codes" in plan.cli_args
    assert "000001,000002,600519,300750" in plan.cli_args
    assert "--start" in plan.cli_args
    assert "20230103" in plan.cli_args
    assert "--end" in plan.cli_args
    assert "20260415" in plan.cli_args
    assert plan.hints["planner"] == "locked_shipped_script"
    assert plan.hints["shipped_script_locked"] is True


@pytest.mark.asyncio
async def test_skill_command_planner_free_shell_allows_generated_script_when_shipped_script_cannot_cover_target():
    loaded_skill = _load_repo_skill("financial-researching")
    llm = _StubPlannerLLM(
        {
            "mode": "generated_script",
            "command": None,
            "entrypoint": ".skill_generated/main.py",
            "cli_args": [],
            "generated_files": [
                {
                    "path": ".skill_generated/main.py",
                    "content": "print('tsla workflow')\n",
                    "description": "Custom TSLA workflow.",
                }
            ],
            "rationale": "The shipped A-share scripts do not cover TSLA cleanly, so generate a custom workflow.",
            "missing_fields": [],
            "failure_reason": None,
            "required_tools": ["python"],
            "warnings": [],
        }
    )
    planner = SkillCommandPlanner(llm_client=llm)

    plan = await planner.plan(
        loaded_skill=loaded_skill,
        request=SkillExecutionRequest(
            skill_name="financial-researching",
            goal="抓取 Tesla(TSLA) 从 2023-01-03 到 2026-04-15 的历史股票数据，并完成端到端建模与回测",
            user_query="free shell 模式下完成 TSLA 的建模回测",
            constraints={"shell_mode": "free_shell"},
        ),
    )

    assert len(llm.calls) == 1
    assert plan.mode == "generated_script"
    assert plan.entrypoint == ".skill_generated/main.py"
    assert plan.command == "python ./.skill_generated/main.py"


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
