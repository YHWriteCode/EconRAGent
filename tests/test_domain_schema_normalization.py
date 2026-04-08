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


def test_domain_schema_relation_keyword_normalization_deduplicates_aliases():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": True}
    ).to_runtime_dict()

    assert (
        normalize_extracted_relation_keywords("政策支持,扶持,支持,景气度", schema)
        == "policy_supports,景气度"
    )
    assert (
        normalize_extracted_relation_keywords("影响,驱动,盈利能力", schema)
        == "affects_metric,盈利能力"
    )


def test_disabled_domain_schema_preserves_original_values():
    schema = resolve_domain_schema(
        {"profile_name": "economy", "enabled": False}
    ).to_runtime_dict()

    assert normalize_extracted_entity_type("公司", schema) == "公司"
    assert normalize_extracted_relation_keywords("政策支持,扶持", schema) == "政策支持,扶持"


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
    assert maybe_edges[("政策", "比亚迪")][0]["keywords"] == "policy_supports"
