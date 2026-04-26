import pytest

from lightrag_fork.operate import _process_extraction_result
from lightrag_fork.schema import (
    normalize_extracted_entity_type,
    normalize_extracted_relation_keywords,
    resolve_domain_schema,
)


def test_domain_schema_entity_type_normalization_uses_builtin_aliases():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()

    assert normalize_extracted_entity_type("公司", schema) == "Company"
    assert normalize_extracted_entity_type("company", schema) == "Company"
    assert normalize_extracted_entity_type("产业", schema) == "Industry"
    assert normalize_extracted_entity_type("国家", schema) == "Location"
    assert normalize_extracted_entity_type("country", schema) == "Location"
    assert normalize_extracted_entity_type("高管", schema) == "Person"
    assert normalize_extracted_entity_type("主题", schema) == "Concept"


def test_economy_schema_uses_strict_schema_managed_entity_canonicalization():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()

    assert "Country" not in schema["entity_type_names"]
    assert {"Location", "Person", "Concept"}.issubset(schema["entity_type_names"])
    assert schema["language"] == "Chinese"
    assert schema["metadata"]["entity_type_canonicalization"] == "strict"
    assert normalize_extracted_entity_type("made_up_llm_type", schema) == "Concept"


def test_domain_schema_relation_keyword_normalization_uses_strict_aliases():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()

    assert (
        normalize_extracted_relation_keywords(
            "政策支持,政策扶持,政策刺激,景气度",
            schema,
        )
        == "policy_supports,景气度"
    )
    assert (
        normalize_extracted_relation_keywords(
            "影响指标,驱动指标,改善指标,盈利能力",
            schema,
        )
        == "affects_metric,盈利能力"
    )
    assert (
        normalize_extracted_relation_keywords(
            "所属行业,行业归属,新能源车",
            schema,
        )
        == "belongs_to_industry,新能源车"
    )
    assert (
        normalize_extracted_relation_keywords(
            "所在国家,所在地区,海外市场",
            schema,
        )
        == "operates_in_location,海外市场"
    )
    assert (
        normalize_extracted_relation_keywords("扶持,支持,影响,属于", schema)
        == "扶持,支持,影响,属于"
    )


def test_disabled_domain_schema_preserves_original_values():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": False}
    ).to_runtime_dict()

    assert normalize_extracted_entity_type("公司", schema) == "公司"
    assert normalize_extracted_relation_keywords("政策支持,扶持", schema) == "政策支持,扶持"


def test_domain_schema_runtime_includes_explanation_profile_with_inheritance():
    general_schema = resolve_domain_schema(
        {"profile_name": "general", "enabled": False}
    ).to_runtime_dict()
    economy_schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()

    general_profile = general_schema["explanation_profile"]
    economy_profile = economy_schema["explanation_profile"]

    assert general_profile["profile_id"] == "general_explainer"
    assert economy_profile["profile_id"] == "economy_explainer"
    assert economy_profile["extends"] == "general_explainer"
    assert "causal_explanation" in economy_profile["supported_intents"]
    assert any(tag["tag_id"] == "policy" for tag in economy_profile["semantic_tags"])
    assert any(tag["tag_id"] == "cause" for tag in economy_profile["semantic_tags"])
    assert any(
        rule["relation_type"] == "affects_metric"
        for rule in economy_profile["relation_semantics"]
    )
    assert any(
        policy["policy_id"] == "economy_causal_strict"
        for policy in economy_profile["evidence_policies"]
    )
    assert any(
        contract["contract_id"] == "economy_causal_contract"
        for contract in economy_profile["output_contracts"]
    )
    assert any(
        scenario["scenario_id"] == "economy_metric_driver_scenario_v1"
        for scenario in economy_profile["scenario_overrides"]
    )
    assert any(
        rule["node_type"] == "Policy"
        for rule in economy_profile["node_role_rules"]
    )
    assert economy_profile["path_constraints"]["default_max_hops"] == 4
    assert economy_profile["path_constraints"]["penalize_repeated_nodes"] is True
    assert (
        economy_profile["guardrails"]["extra_flags"]["disallow_metric_claim_without_support"]
        is True
    )


@pytest.mark.asyncio
async def test_extraction_result_applies_domain_schema_normalization():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()
    extraction_result = "\n".join(
        [
            'entity<|#|>"比亚迪"<|#|>"公司"<|#|>"中国新能源汽车公司"',
            'relation<|#|>"政策"<|#|>"比亚迪"<|#|>"政策支持,扶持"<|#|>"政策对公司经营形成支持"',
            "<|COMPLETE|>",
        ]
    )

    maybe_nodes, maybe_edges = await _process_extraction_result(
        extraction_result,
        chunk_key="chunk-1",
        timestamp=1710000000,
        file_path="demo.md",
        domain_schema=schema,
    )

    assert maybe_nodes["比亚迪"][0]["entity_type"] == "Company"
    assert maybe_edges[("政策", "比亚迪")][0]["keywords"] == "policy_supports,扶持"
