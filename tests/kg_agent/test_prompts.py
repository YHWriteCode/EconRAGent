from kg_agent.agent.prompts import (
    DEFAULT_PATH_EXPLAINER_TEMPLATE_ID,
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
