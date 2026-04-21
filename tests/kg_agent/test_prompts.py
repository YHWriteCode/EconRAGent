from kg_agent.agent.prompts import (
    DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
    build_final_answer_prompt,
    build_skill_constraint_inference_prompt,
    build_skill_catalog_selector_prompt,
    build_skill_free_shell_planner_prompt,
    build_path_explainer_prompt,
    list_path_explainer_prompt_templates,
    resolve_path_explainer_prompt_template,
)


def test_path_explainer_prompt_template_registry_and_default_fallback():
    templates = list_path_explainer_prompt_templates()

    assert DEFAULT_PATH_EXPLAINER_TEMPLATE_ID in templates
    assert "economy_causal_v1" in templates

    template = resolve_path_explainer_prompt_template("missing-template")

    assert template.template_id == DEFAULT_PATH_EXPLAINER_TEMPLATE_ID


def test_path_explainer_prompt_template_can_fall_back_by_intent_family():
    template = resolve_path_explainer_prompt_template(
        "missing-template",
        intent_family="containment_trace",
    )

    assert template.template_id == "intent_containment_v1"
    assert "membership" in template.description.lower()


def test_build_path_explainer_prompt_uses_economy_template_guidance():
    system_prompt, user_prompt = build_path_explainer_prompt(
        query="政策如何影响比亚迪利润？",
        graph_paths=[
            {
                "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪利润",
                "nodes": [],
                "edges": [],
            }
        ],
        evidence_chunks=["政策支持推动新能源汽车行业扩张。"],
        domain_schema={"profile_name": "economy"},
        explanation_profile={"profile_id": "economy_explainer"},
        intent_family="causal_explanation",
        template_id="economy_causal_v1",
        scenario_id="economy_policy_impact_scenario_v1",
        scenario_override={"scenario_id": "economy_policy_impact_scenario_v1"},
        evidence_policy={"policy_id": "economy_causal_strict"},
        output_contract={"contract_id": "economy_causal_contract"},
        guardrails={"extra_flags": {"disallow_metric_claim_without_support": True}},
    )

    assert "economy and finance" in system_prompt.lower()
    assert "Resolved scenario id:\neconomy_policy_impact_scenario_v1" in user_prompt
    assert "Resolved prompt template:\neconomy_causal_v1" in user_prompt
    assert "driver -> transmission channel -> metric or company outcome" in user_prompt


def test_build_route_judge_prompt_includes_skills_and_skill_plan_contract():
    from kg_agent.agent.prompts import build_route_judge_prompt

    system_prompt, user_prompt = build_route_judge_prompt(
        query="clean this spreadsheet",
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
                "tags": ["spreadsheet", "xlsx"],
                "path": "/skills/xlsx",
            }
        ],
        current_plan={"strategy": "skill_request"},
    )

    assert "route judge" in system_prompt.lower()
    assert "Available skills:" in user_prompt
    assert '"skill_plan": {"skill_name": str, "goal": str, "reason": str, "constraints": dict} | null' in user_prompt
    assert "include structured constraints when they are explicit in the user query" in user_prompt


def test_build_skill_catalog_selector_prompt_uses_skill_metadata_as_primary_surface():
    system_prompt, user_prompt = build_skill_catalog_selector_prompt(
        query="我家手工做了不少艾草糯米米果，应该怎么定价才更合适？",
        available_skills=[
            {
                "name": "pricing",
                "description": "Help figure out pricing for a product or service.",
                "tags": ["pricing"],
                "path": "/skills/pricing",
            }
        ],
        current_plan={"strategy": "factual_qa"},
    )

    assert "semantic skill selector" in system_prompt.lower()
    assert "Cross-lingual semantic matching is allowed." in system_prompt
    assert "Do not require exact name overlap" in system_prompt
    assert "Available skills:" in user_prompt
    assert '"skill_name": str | null' in user_prompt


def test_build_final_answer_prompt_includes_capability_and_skill_catalogs():
    system_prompt, user_prompt = build_final_answer_prompt(
        query="你有哪些工具和技能？",
        route={"strategy": "agent_metadata_answer"},
        tool_results=[],
        path_explanation=None,
        conversation_history=[],
        available_capability_catalog=[
            {"name": "kg_hybrid_search", "description": "Run LightRAG hybrid retrieval."}
        ],
        available_skills=[
            {
                "name": "financial-researching",
                "description": "Analyze stock trends and financial data.",
            }
        ],
    )

    assert "capability and skill catalogs" in system_prompt
    assert "Available capabilities:" in user_prompt
    assert "kg_hybrid_search" in user_prompt
    assert "Available skills:" in user_prompt
    assert "financial-researching" in user_prompt
    assert "manual_required_kind='technical_blocked'" in system_prompt
    assert "do not add generic retry advice" in system_prompt
    assert "advisory_mode='doc_only'" in system_prompt
    assert "reasoned recommendations, estimates, or suggested ranges" in system_prompt


def test_build_skill_constraint_inference_prompt_includes_reference_date_and_allowed_keys():
    system_prompt, user_prompt = build_skill_constraint_inference_prompt(
        skill_name="financial-researching",
        goal="分析比亚迪过去一年波动和最近一个季度趋势",
        user_query="请帮我看一下比亚迪 002594 从去年同期到现在的波动情况，并判断最近一个季度是否上行",
        reference_date="2026-04-16",
        runtime_target={"platform": "linux", "shell": "/bin/sh"},
        current_constraints={"code": "002594"},
        allowed_constraint_keys=["code", "start", "end", "trend_start", "trend_end", "output_path"],
        skill_md_excerpt="Use analyze_stock_trend.py for single-stock volatility and trend analysis.",
        script_previews=[{"path": "scripts/analyze_stock_trend.py", "score": 9}],
        file_inventory=[{"path": "scripts/analyze_stock_trend.py", "kind": "python", "size_bytes": 1024}],
    )

    assert "structured skill constraints" in system_prompt.lower()
    assert "Reference date:\n2026-04-16" in user_prompt
    assert '"trend_start"' in user_prompt
    assert '"trend_end"' in user_prompt


def test_build_skill_free_shell_prompt_uses_full_skill_md_and_natural_language_dependency_guidance():
    system_prompt, user_prompt = build_skill_free_shell_planner_prompt(
        skill_name="pptx",
        goal="Create a presentation",
        user_query="做一个PPT",
        runtime_target={"platform": "linux", "shell": "/bin/sh"},
        constraints={"shell_mode": "free_shell"},
        effective_constraints={"shell_mode": "free_shell", "topic": "生成式人工智能"},
        shell_hints={"required_credentials": [], "runnable_scripts": ["scripts/build.py"]},
        file_inventory=[{"path": "SKILL.md", "kind": "skill_doc", "size_bytes": 1024}],
        doc_bundle=[
            {
                "path": "SKILL.md",
                "hop": 0,
                "source_path": None,
                "score": 100,
                "content": "Line 1\nLine 2\nInstall with pip install example-package",
            },
            {
                "path": "pptxgenjs.md",
                "hop": 1,
                "source_path": "SKILL.md",
                "score": 90,
                "content": "Use pptxgenjs for create-from-scratch workflows.",
            },
        ],
        cli_history=[],
        script_previews=[],
        python_examples=[],
        conservative_plan={"mode": "manual_required"},
    )

    assert "free-shell planning module" in system_prompt.lower()
    assert "Progressive skill document bundle:" in user_prompt
    assert "Effective structured constraints after auto-fill:" in user_prompt
    assert "natural-language dependency guidance anywhere in SKILL.md" in user_prompt
    assert "SKILL_BOOTSTRAP_SITE_PACKAGES" in user_prompt
    assert "NPM_CONFIG_PREFIX" in user_prompt
    assert "If the runtime target only guarantees Python, prefer Python-based generated scripts" in user_prompt
    assert "use npm install -g pptxgenjs" in user_prompt
    assert "pip install example-package" in user_prompt
    assert "pptxgenjs.md" in user_prompt
