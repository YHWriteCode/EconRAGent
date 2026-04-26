import pytest

from kg_agent.agent.path_explainer import (
    PathExplainer,
    _resolve_evidence_policy,
    _resolve_explanation_profile,
    _resolve_output_contract,
    _resolve_query_intent,
    _resolve_scenario_override,
    _tokenize,
)
from lightrag_fork.schema import resolve_domain_schema


@pytest.mark.asyncio
async def test_path_explainer_returns_best_path_with_evidence():
    explainer = PathExplainer()
    graph_paths = [
        {
            "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪",
            "nodes": [{"id": "政策支持"}, {"id": "新能源汽车行业"}, {"id": "比亚迪"}],
            "edges": [{"source": "政策支持", "target": "新能源汽车行业"}],
        },
        {
            "path_text": "锂价 -> 成本压力 -> 比亚迪",
            "nodes": [{"id": "锂价"}, {"id": "成本压力"}, {"id": "比亚迪"}],
            "edges": [{"source": "锂价", "target": "成本压力"}],
        },
    ]
    evidence = [
        "政策支持推动了新能源汽车行业扩张，并带动比亚迪订单增长。",
        "锂价波动会影响电池材料成本。",
    ]

    result = await explainer.explain(
        query="比亚迪受新能源汽车政策影响体现在哪些方面？",
        graph_paths=graph_paths,
        evidence_chunks=evidence,
    )

    assert result.enabled is True
    assert len(result.paths) == 1
    assert result.paths[0].path_text == "政策支持 -> 新能源汽车行业 -> 比亚迪"
    assert "比亚迪" in result.final_explanation


@pytest.mark.asyncio
async def test_path_explainer_falls_back_without_evidence():
    explainer = PathExplainer()

    result = await explainer.explain(
        query="为什么会这样？",
        graph_paths=[{"path_text": "A -> B", "nodes": [], "edges": []}],
        evidence_chunks=[],
    )

    assert result.enabled is False
    assert result.paths == []


def test_tokenize_adds_cjk_entity_tokens_and_relation_tags():
    tokens = _tokenize("政策变化如何影响比亚迪利润表现？")

    assert "比亚迪" in tokens
    assert "利润" in tokens
    assert "__impact__" in tokens
    assert "__policy__" in tokens


def test_tokenize_uses_explanation_profile_semantic_tags():
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    explanation_profile = _resolve_explanation_profile(economy_schema)

    tokens = _tokenize("收入增长原因是什么？", explanation_profile)

    assert "__metric__" in tokens
    assert "__increase__" in tokens
    assert "__cause__" in tokens


def test_resolve_query_intent_uses_explanation_profile_triggers():
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    explanation_profile = _resolve_explanation_profile(economy_schema)

    intent = _resolve_query_intent("政策如何影响比亚迪利润？", explanation_profile)

    assert intent is not None
    assert intent["intent_family"] == "causal_explanation"
    assert intent["template_id"] == "economy_causal_v1"


def test_resolve_scenario_override_uses_builtin_metric_driver_override():
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    explanation_profile = _resolve_explanation_profile(economy_schema)

    intent = _resolve_query_intent("收入增长原因是什么？", explanation_profile)
    scenario = _resolve_scenario_override(explanation_profile, intent)
    evidence_policy = _resolve_evidence_policy(explanation_profile, intent, scenario)
    output_contract = _resolve_output_contract(explanation_profile, intent, scenario)

    assert intent is not None
    assert intent["scenario_id"] == "economy_metric_driver_scenario_v1"
    assert scenario is not None
    assert scenario["template_id"] == "economy_metric_driver_v1"
    assert evidence_policy is not None
    assert evidence_policy["policy_id"] == "economy_causal_strict"
    assert output_contract is not None
    assert output_contract["contract_id"] == "economy_metric_driver_contract"


@pytest.mark.asyncio
async def test_path_explainer_prefers_semantically_supported_path():
    explainer = PathExplainer()
    graph_paths = [
        {
            "path_text": "政策调整 -> 新能源汽车行业 -> 比亚迪订单",
            "nodes": [{"id": "政策调整"}, {"id": "新能源汽车行业"}, {"id": "比亚迪订单"}],
            "edges": [{"source": "政策调整", "target": "新能源汽车行业"}],
        },
        {
            "path_text": "比亚迪 -> 海外市场 -> 表现",
            "nodes": [{"id": "比亚迪"}, {"id": "海外市场"}, {"id": "表现"}],
            "edges": [{"source": "比亚迪", "target": "海外市场"}],
        },
    ]
    evidence = [
        "政策调整推动新能源汽车行业需求扩张，并带动比亚迪订单改善。",
        "比亚迪海外市场表现稳定。",
    ]

    result = await explainer.explain(
        query="政策变化如何影响比亚迪订单增长？",
        graph_paths=graph_paths,
        evidence_chunks=evidence,
    )

    assert result.enabled is True
    assert result.paths[0].path_text == "政策调整 -> 新能源汽车行业 -> 比亚迪订单"
    assert result.paths[0].evidence[0] == evidence[0]


def test_path_explainer_penalizes_overlong_paths():
    explainer = PathExplainer()
    evidence = ["锂价上涨推高电池材料成本，压缩比亚迪利润空间。"]
    short_path = {
        "path_text": "锂价上涨 -> 成本压力 -> 比亚迪利润",
        "nodes": [{"id": "锂价上涨"}, {"id": "成本压力"}, {"id": "比亚迪利润"}],
        "edges": [{"source": "锂价上涨", "target": "成本压力"}],
    }
    long_path = {
        "path_text": "锂价上涨 -> 上游资源供给 -> 中游材料厂 -> 电池成本 -> 整车毛利 -> 比亚迪利润",
        "nodes": [
            {"id": "锂价上涨"},
            {"id": "上游资源供给"},
            {"id": "中游材料厂"},
            {"id": "电池成本"},
            {"id": "整车毛利"},
            {"id": "比亚迪利润"},
        ],
        "edges": [
            {"source": "锂价上涨", "target": "上游资源供给"},
            {"source": "上游资源供给", "target": "中游材料厂"},
            {"source": "中游材料厂", "target": "电池成本"},
            {"source": "电池成本", "target": "整车毛利"},
            {"source": "整车毛利", "target": "比亚迪利润"},
        ],
    }

    assert explainer._score_path(
        "锂价上涨如何影响比亚迪利润？", short_path, evidence
    ) > explainer._score_path("锂价上涨如何影响比亚迪利润？", long_path, evidence)


@pytest.mark.asyncio
async def test_path_explainer_keeps_question_type_compatible_with_profile_intent():
    explainer = PathExplainer()
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    graph_paths = [
        {
            "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪",
            "nodes": [{"id": "政策支持"}, {"id": "新能源汽车行业"}, {"id": "比亚迪"}],
            "edges": [{"source": "政策支持", "target": "新能源汽车行业"}],
        }
    ]
    evidence = ["政策支持推动了新能源汽车行业扩张，并带动比亚迪订单增长。"]

    result = await explainer.explain(
        query="政策如何影响比亚迪？",
        graph_paths=graph_paths,
        evidence_chunks=evidence,
        domain_schema=economy_schema,
    )

    assert result.enabled is True
    assert result.question_type == "relation_explanation"


def test_path_explainer_scores_relation_semantics_fit():
    explainer = PathExplainer()
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    explanation_profile = _resolve_explanation_profile(economy_schema)
    intent = _resolve_query_intent("政策如何影响比亚迪收入增长？", explanation_profile)
    evidence_policy = _resolve_evidence_policy(explanation_profile, intent)
    evidence = ["政策支持推动比亚迪收入增长。"]
    matched_path = {
        "path_text": "政策支持 -> 比亚迪收入",
        "nodes": [{"id": "政策支持"}, {"id": "比亚迪收入"}],
        "edges": [{"type": "policy_supports", "source": "政策支持", "target": "比亚迪收入"}],
    }
    mismatched_path = {
        "path_text": "政策支持 -> 比亚迪收入",
        "nodes": [{"id": "政策支持"}, {"id": "比亚迪收入"}],
        "edges": [{"type": "belongs_to_industry", "source": "政策支持", "target": "比亚迪收入"}],
    }

    assert explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        matched_path,
        evidence,
        explanation_profile=explanation_profile,
        intent_binding=intent,
        intent_family="causal_explanation",
        evidence_policy=evidence_policy,
    ) > explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        mismatched_path,
        evidence,
        explanation_profile=explanation_profile,
        intent_binding=intent,
        intent_family="causal_explanation",
        evidence_policy=evidence_policy,
    )


def test_path_explainer_scores_node_role_alignment_for_economy_paths():
    explainer = PathExplainer()
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    explanation_profile = _resolve_explanation_profile(economy_schema)
    intent = _resolve_query_intent("政策如何影响比亚迪收入增长？", explanation_profile)
    evidence_policy = _resolve_evidence_policy(explanation_profile, intent)
    evidence = ["政策支持推动新能源汽车行业需求扩张，并带动比亚迪收入增长。"]
    aligned_path = {
        "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪收入",
        "nodes": [
            {"id": "政策支持", "entity_type": "Policy"},
            {"id": "新能源汽车行业", "entity_type": "Industry"},
            {"id": "比亚迪收入", "entity_type": "Metric"},
        ],
        "edges": [
            {"type": "policy_supports", "source": "政策支持", "target": "新能源汽车行业"},
            {"type": "affects_metric", "source": "新能源汽车行业", "target": "比亚迪收入"},
        ],
    }
    misaligned_path = {
        "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪收入",
        "nodes": [
            {"id": "政策支持", "entity_type": "Location"},
            {"id": "新能源汽车行业", "entity_type": "Location"},
            {"id": "比亚迪收入", "entity_type": "Industry"},
        ],
        "edges": [
            {"type": "policy_supports", "source": "政策支持", "target": "新能源汽车行业"},
            {"type": "affects_metric", "source": "新能源汽车行业", "target": "比亚迪收入"},
        ],
    }

    assert explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        aligned_path,
        evidence,
        explanation_profile=explanation_profile,
        intent_binding=intent,
        intent_family="causal_explanation",
        evidence_policy=evidence_policy,
    ) > explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        misaligned_path,
        evidence,
        explanation_profile=explanation_profile,
        intent_binding=intent,
        intent_family="causal_explanation",
        evidence_policy=evidence_policy,
    )


def test_path_explainer_respects_tighter_path_constraints():
    explainer = PathExplainer()
    default_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    tight_schema = resolve_domain_schema(
        {
            "profile_name": "economy",
            "enabled": True,
            "explanation_profile": {
                "path_constraints": {
                    "default_max_hops": 2,
                    "prefer_shorter_paths": True,
                    "penalize_repeated_nodes": False,
                }
            },
        }
    ).to_runtime_dict()
    default_profile = _resolve_explanation_profile(default_schema)
    tight_profile = _resolve_explanation_profile(tight_schema)
    default_intent = _resolve_query_intent("政策如何影响比亚迪收入增长？", default_profile)
    tight_intent = _resolve_query_intent("政策如何影响比亚迪收入增长？", tight_profile)
    default_policy = _resolve_evidence_policy(default_profile, default_intent)
    tight_policy = _resolve_evidence_policy(tight_profile, tight_intent)
    evidence = ["政策支持推动新能源汽车行业需求扩张，并带动比亚迪收入增长。"]
    long_path = {
        "path_text": "政策支持 -> 新能源汽车行业 -> 产业链景气度 -> 市场需求 -> 比亚迪收入",
        "nodes": [
            {"id": "政策支持", "entity_type": "Policy"},
            {"id": "新能源汽车行业", "entity_type": "Industry"},
            {"id": "产业链景气度", "entity_type": "Industry"},
            {"id": "市场需求", "entity_type": "Event"},
            {"id": "比亚迪收入", "entity_type": "Metric"},
        ],
        "edges": [
            {"type": "policy_supports", "source": "政策支持", "target": "新能源汽车行业"},
            {"type": "policy_supports", "source": "新能源汽车行业", "target": "产业链景气度"},
            {"type": "policy_supports", "source": "产业链景气度", "target": "市场需求"},
            {"type": "affects_metric", "source": "市场需求", "target": "比亚迪收入"},
        ],
    }

    assert explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        long_path,
        evidence,
        explanation_profile=tight_profile,
        intent_binding=tight_intent,
        intent_family="causal_explanation",
        evidence_policy=tight_policy,
    ) < explainer._score_path(
        "政策如何影响比亚迪收入增长？",
        long_path,
        evidence,
        explanation_profile=default_profile,
        intent_binding=default_intent,
        intent_family="causal_explanation",
        evidence_policy=default_policy,
    )


@pytest.mark.asyncio
async def test_path_explainer_respects_min_supporting_chunks_policy():
    explainer = PathExplainer()
    economy_schema = resolve_domain_schema(
        {
            "profile_name": "economy",
            "enabled": True,
            "explanation_profile": {
                "intent_bindings": [
                    {
                        "intent_id": "economy_metric_driver",
                        "intent_family": "causal_explanation",
                        "triggers": [
                            "利润为什么变化",
                            "毛利率为何变化",
                            "收入增长原因",
                            "what drives revenue",
                            "what drives margin",
                        ],
                        "preferred_semantic_tags": ["metric", "cost", "risk", "impact"],
                        "preferred_relation_types": ["affects_metric"],
                        "evidence_policy_id": "strict_two_chunks",
                        "output_contract_id": "economy_causal_contract",
                        "template_id": "economy_metric_driver_v1",
                    }
                ],
                "evidence_policies": [
                    {
                        "policy_id": "strict_two_chunks",
                        "applies_to_intents": ["causal_explanation"],
                        "require_evidence": True,
                        "require_per_hop_support": False,
                        "min_supporting_chunks": 2,
                        "allow_partial_support": False,
                        "citation_granularity": "path",
                    }
                ],
            },
        }
    ).to_runtime_dict()

    result = await explainer.explain(
        query="收入增长原因是什么？",
        graph_paths=[
            {
                "path_text": "政策支持 -> 比亚迪收入",
                "nodes": [{"id": "政策支持"}, {"id": "比亚迪收入"}],
                "edges": [{"type": "affects_metric", "source": "政策支持", "target": "比亚迪收入"}],
            }
        ],
        evidence_chunks=["政策支持推动比亚迪收入增长。"],
        domain_schema=economy_schema,
    )

    assert result.enabled is False


@pytest.mark.asyncio
async def test_path_explainer_emits_uncertainty_when_partial_support_is_allowed():
    explainer = PathExplainer()
    economy_schema = resolve_domain_schema(
        {
            "profile_name": "economy",
            "enabled": True,
            "explanation_profile": {
                "intent_bindings": [
                    {
                        "intent_id": "economy_policy_impact",
                        "intent_family": "causal_explanation",
                        "triggers": [
                            "政策影响",
                            "政策如何影响",
                            "补贴影响",
                            "监管影响",
                            "policy impact",
                            "regulation affect",
                        ],
                        "negative_triggers": ["属于行业", "公司简介"],
                        "preferred_semantic_tags": ["policy", "impact", "metric"],
                        "preferred_relation_types": ["policy_supports", "affects_metric"],
                        "evidence_policy_id": "partial_causal",
                        "output_contract_id": "partial_contract",
                        "template_id": "economy_causal_v1",
                    }
                ],
                "evidence_policies": [
                    {
                        "policy_id": "partial_causal",
                        "applies_to_intents": ["causal_explanation"],
                        "require_evidence": True,
                        "require_per_hop_support": True,
                        "min_supporting_chunks": 1,
                        "allow_partial_support": True,
                        "citation_granularity": "path",
                    }
                ],
                "output_contracts": [
                    {
                        "contract_id": "partial_contract",
                        "applies_to_intents": ["causal_explanation"],
                        "required_sections": [
                            "answer",
                            "causal_chain",
                            "evidence_summary",
                            "uncertainty",
                        ],
                        "optional_sections": [],
                        "require_uncertainty_when_partial": True,
                    }
                ],
                "guardrails": {
                    "require_explicit_uncertainty_on_partial_support": True,
                    "extra_flags": {
                        "disallow_metric_claim_without_support": True,
                    },
                },
            },
        }
    ).to_runtime_dict()

    result = await explainer.explain(
        query="政策如何影响比亚迪利润？",
        graph_paths=[
            {
                "path_text": "政策支持 -> 新能源汽车行业 -> 比亚迪利润",
                "nodes": [
                    {"id": "政策支持"},
                    {"id": "新能源汽车行业"},
                    {"id": "比亚迪利润"},
                ],
                "edges": [
                    {
                        "type": "policy_supports",
                        "source": "政策支持",
                        "target": "新能源汽车行业",
                    },
                    {
                        "type": "affects_metric",
                        "source": "新能源汽车行业",
                        "target": "比亚迪利润",
                    },
                ],
            }
        ],
        evidence_chunks=["政策支持推动新能源汽车行业扩张。"],
        domain_schema=economy_schema,
    )

    assert result.enabled is True
    assert result.uncertainty is not None
    assert "partially supported" in result.uncertainty.lower()
