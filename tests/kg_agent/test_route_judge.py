import pytest

from kg_agent.agent.route_judge import RouteJudge
from kg_agent.agent.prompts import (
    DEFAULT_ROUTE_JUDGE_PROMPT_VERSION,
    build_route_judge_prompt,
    list_route_judge_prompt_versions,
)
from kg_agent.skills.registry import SkillRegistry


@pytest.mark.asyncio
async def test_route_judge_relation_explanation_prefers_hybrid_and_graph_trace():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search", "graph_relation_trace"],
    )

    assert route.need_tools is True
    assert route.need_path_explanation is True
    assert [item.tool for item in route.tool_sequence] == [
        "kg_hybrid_search",
        "graph_relation_trace",
    ]


@pytest.mark.asyncio
async def test_route_judge_followup_prefers_memory_first():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="继续刚才关于比亚迪的话题",
        session_context={"history": [{"role": "user", "content": "比亚迪是什么公司"}]},
        user_profile={},
        available_capabilities=["memory_search", "kg_hybrid_search"],
    )

    assert route.need_memory is True
    assert route.tool_sequence[0].tool == "memory_search"


@pytest.mark.asyncio
async def test_route_judge_followup_can_use_cross_session_memory():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="continue the previous supplier topic",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["cross_session_search", "kg_hybrid_search"],
    )

    assert route.need_memory is True
    assert route.tool_sequence[0].tool == "cross_session_search"


@pytest.mark.asyncio
async def test_route_judge_quant_request_is_marked_as_external_specialized_capability():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="帮我回测一个双均线策略，并给出夏普比率",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search", "web_search"],
    )

    assert route.strategy == "specialized_external_capability"
    assert route.need_tools is False
    assert route.tool_sequence == []


@pytest.mark.asyncio
async def test_route_judge_skill_aware_catalog_selects_matching_external_skill():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="run a backtest for AAPL and show sharpe ratio",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search", "quant_backtest_skill"],
        available_capability_catalog=[
            {
                "name": "kg_hybrid_search",
                "description": "Run LightRAG hybrid retrieval.",
                "kind": "native",
                "executor": "tool_registry",
                "tags": ["retrieval"],
                "arg_names": ["query"],
                "required_args": ["query"],
            },
            {
                "name": "quant_backtest_skill",
                "description": "Run a backtest through an external MCP skill.",
                "kind": "external_mcp",
                "executor": "mcp",
                "tags": ["quant", "skill"],
                "arg_names": ["query", "symbol"],
                "required_args": ["query"],
            },
        ],
    )

    assert route.strategy == "external_capability_request"
    assert route.need_tools is True
    assert [item.tool for item in route.tool_sequence] == ["quant_backtest_skill"]
    assert route.skill_plan is None


@pytest.mark.asyncio
async def test_route_judge_selects_local_skill_from_available_skills():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="clean this xlsx spreadsheet and keep formulas intact",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "xlsx",
                "description": "Use this skill any time a spreadsheet file is the primary input or output.",
                "tags": ["spreadsheet", "xlsx", "csv"],
                "path": "/skills/xlsx",
            }
        ],
    )

    assert route.strategy == "skill_request"
    assert route.need_tools is False
    assert route.tool_sequence == []
    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "xlsx"
    assert "xlsx" in route.skill_plan.goal.lower()


@pytest.mark.asyncio
async def test_route_judge_selects_financial_researching_skill_for_chinese_stock_analysis():
    judge = RouteJudge(default_max_iterations=3)
    skill_registry = SkillRegistry("skills")

    route = await judge.plan(
        query="请帮我查找以下最近一年比亚迪002594股票的波动情况，3个月内是否有上涨趋势",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            skill.to_catalog_dict() for skill in skill_registry.refresh()
        ],
    )

    assert route.strategy == "skill_request"
    assert route.need_tools is False
    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "financial-researching"


@pytest.mark.asyncio
async def test_route_judge_extracts_spreadsheet_constraints_for_xlsx_skill():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query='recalculate formulas in "C:\\Reports\\model.xlsx" and keep formulas intact',
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "xlsx",
                "description": "Use this skill any time a spreadsheet file is the primary input or output.",
                "tags": ["spreadsheet", "xlsx", "csv"],
                "path": "/skills/xlsx",
            }
        ],
    )

    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "xlsx"
    assert route.skill_plan.constraints["input_path"] == "C:\\Reports\\model.xlsx"
    assert route.skill_plan.constraints["operation"] == "recalc"
    assert route.skill_plan.constraints["preserve_formulas"] is True


@pytest.mark.asyncio
async def test_route_judge_does_not_use_legacy_skill_helper_tools_as_primary_surface():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="please run a backtest workflow for AAPL",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["list_skills", "read_skill_docs", "execute_skill_script"],
        available_capability_catalog=[
            {
                "name": "list_skills",
                "description": "List available MCP-hosted agent skills.",
                "kind": "external_mcp",
                "executor": "mcp",
                "tags": ["mcp", "skill-catalog"],
                "arg_names": ["query"],
                "required_args": [],
            },
            {
                "name": "read_skill_docs",
                "description": "Load SKILL.md and references for a selected skill.",
                "kind": "external_mcp",
                "executor": "mcp",
                "tags": ["mcp", "skill-docs"],
                "arg_names": ["skill_name"],
                "required_args": ["skill_name"],
            },
            {
                "name": "execute_skill_script",
                "description": "Execute the selected skill's script in an isolated workspace.",
                "kind": "external_mcp",
                "executor": "mcp",
                "tags": ["mcp", "skill-exec"],
                "arg_names": ["skill_name", "script_name"],
                "required_args": ["skill_name", "script_name"],
            },
        ],
    )

    assert route.strategy == "specialized_external_capability"
    assert route.need_tools is False
    assert route.tool_sequence == []
    assert route.skill_plan is None


@pytest.mark.asyncio
async def test_route_judge_direct_url_prefers_web_crawl():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="请帮我分析这个网页 https://example.com/news/byd-policy 的内容",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["web_search", "kg_hybrid_search"],
    )

    assert route.strategy == "direct_url_crawl"
    assert route.tool_sequence[0].tool == "web_search"
    assert route.tool_sequence[0].args["urls"] == [
        "https://example.com/news/byd-policy"
    ]


class _CapturingLLMClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def is_available(self):
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


@pytest.mark.asyncio
async def test_route_judge_llm_refinement_uses_selected_prompt_version():
    llm_client = _CapturingLLMClient(
        {
            "need_tools": True,
            "need_memory": False,
            "need_web_search": False,
            "need_path_explanation": True,
            "strategy": "kg_hybrid_first_then_graph_trace",
            "tool_sequence": [
                {"tool": "kg_hybrid_search", "args": {}, "optional": False},
                {"tool": "graph_relation_trace", "args": {}, "optional": False},
            ],
            "reason": "keep relation route",
            "max_iterations": 3,
        }
    )
    judge = RouteJudge(
        llm_client=llm_client,
        default_max_iterations=3,
        prompt_version="v2",
    )

    route = await judge.plan(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        session_context={"history": []},
        user_profile={"industry": "auto"},
        available_capabilities=["kg_hybrid_search", "graph_relation_trace"],
        available_capability_catalog=[
            {
                "name": "kg_hybrid_search",
                "description": "Run LightRAG hybrid retrieval and return structured entities, relations, and chunks.",
                "kind": "native",
                "executor": "tool_registry",
                "tags": ["retrieval", "knowledge-graph"],
                "arg_names": ["query", "mode", "top_k", "chunk_top_k"],
                "required_args": ["query"],
            },
            {
                "name": "graph_relation_trace",
                "description": "Trace short candidate paths around a graph entity for explanation.",
                "kind": "native",
                "executor": "tool_registry",
                "tags": ["graph", "explanation"],
                "arg_names": ["query", "entity_name", "max_depth", "max_paths"],
                "required_args": ["query"],
            },
        ],
    )

    assert route.strategy == "kg_hybrid_first_then_graph_trace"
    assert judge.prompt_version == "v2"
    assert len(llm_client.calls) == 1
    assert "Prompt version: v2" in llm_client.calls[0]["user_prompt"]
    assert "Refinement policy:" in llm_client.calls[0]["user_prompt"]
    assert "Capability catalog:" in llm_client.calls[0]["user_prompt"]
    assert "Available skills:" in llm_client.calls[0]["user_prompt"]
    assert "Trace short candidate paths around a graph entity for explanation." in (
        llm_client.calls[0]["user_prompt"]
    )


def test_route_judge_prompt_version_registry_and_fallback():
    versions = list_route_judge_prompt_versions()

    assert DEFAULT_ROUTE_JUDGE_PROMPT_VERSION in versions
    assert "v2" in versions

    judge = RouteJudge(prompt_version="missing-version")
    assert judge.prompt_version == DEFAULT_ROUTE_JUDGE_PROMPT_VERSION

    system_prompt, user_prompt = build_route_judge_prompt(
        query="supplier update",
        session_context={"history": []},
        available_capabilities=["kg_hybrid_search"],
        available_capability_catalog=[
            {
                "name": "kg_hybrid_search",
                "description": "Run LightRAG hybrid retrieval.",
                "kind": "native",
                "executor": "tool_registry",
                "tags": ["retrieval"],
                "arg_names": ["query"],
                "required_args": ["query"],
            }
        ],
        available_skills=[
            {
                "name": "xlsx",
                "description": "Spreadsheet workflow skill.",
                "tags": ["spreadsheet"],
                "path": "/skills/xlsx",
            }
        ],
        current_plan={"strategy": "factual_qa"},
        prompt_version="missing-version",
    )

    assert "route judge" in system_prompt.lower()
    assert f"Prompt version: {DEFAULT_ROUTE_JUDGE_PROMPT_VERSION}" in user_prompt
    assert "Available capabilities:" in user_prompt
    assert "Capability catalog:" in user_prompt
    assert "Available skills:" in user_prompt


@pytest.mark.asyncio
async def test_route_judge_extracts_cli_args_output_path_mode_format_and_dry_run():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query=(
            'use report-skill --input "C:\\Reports\\input.csv" --output '
            '"C:\\Reports\\out.md" --format markdown --mode strict --dry-run'
        ),
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "report-skill",
                "description": "Create a report workflow from structured inputs.",
                "tags": ["report", "workflow"],
                "path": "/skills/report-skill",
            }
        ],
    )

    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "report-skill"
    assert route.skill_plan.constraints["input_path"] == "C:\\Reports\\input.csv"
    assert route.skill_plan.constraints["output_path"] == "C:\\Reports\\out.md"
    assert route.skill_plan.constraints["format"] == "markdown"
    assert route.skill_plan.constraints["output_format"] == "markdown"
    assert route.skill_plan.constraints["mode"] == "strict"
    assert route.skill_plan.constraints["dry_run"] is True
    assert route.skill_plan.constraints["plan_only"] is True
    assert route.skill_plan.constraints["cli_args"] == [
        "--input",
        "C:\\Reports\\input.csv",
        "--output",
        "C:\\Reports\\out.md",
        "--format",
        "markdown",
        "--mode",
        "strict",
        "--dry-run",
    ]


@pytest.mark.asyncio
async def test_route_judge_extracts_free_shell_mode_hint():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="use pdf skill in free shell mode to merge the files",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "pdf",
                "description": "Process pdf files.",
                "tags": ["pdf"],
                "path": "/skills/pdf",
            }
        ],
    )

    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "pdf"
    assert route.skill_plan.constraints["shell_mode"] == "free_shell"


@pytest.mark.asyncio
async def test_route_judge_infers_free_shell_for_generate_script_request():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="use pdf skill to write a helper script first and then execute it on these files",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "pdf",
                "description": "Process pdf files.",
                "tags": ["pdf"],
                "path": "/skills/pdf",
            }
        ],
    )

    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "pdf"
    assert route.skill_plan.constraints["shell_mode"] == "free_shell"


@pytest.mark.asyncio
async def test_route_judge_infers_free_shell_for_complex_command_request():
    judge = RouteJudge(default_max_iterations=3)

    route = await judge.plan(
        query="use pdf skill with a complex command pipeline to process these files",
        session_context={"history": []},
        user_profile={},
        available_capabilities=["kg_hybrid_search"],
        available_skills=[
            {
                "name": "pdf",
                "description": "Process pdf files.",
                "tags": ["pdf"],
                "path": "/skills/pdf",
            }
        ],
    )

    assert route.skill_plan is not None
    assert route.skill_plan.skill_name == "pdf"
    assert route.skill_plan.constraints["shell_mode"] == "free_shell"
