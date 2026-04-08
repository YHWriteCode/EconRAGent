from __future__ import annotations

from lightrag_fork.constants import DEFAULT_SUMMARY_LANGUAGE
from lightrag_fork.schema import (
    DomainSchema,
    EvidencePolicyDefinition,
    EntityTypeDefinition,
    ExplanationProfile,
    GuardrailPolicy,
    IntentBindingDefinition,
    NodeRoleRuleDefinition,
    OutputContractDefinition,
    PathConstraintPolicy,
    PromptBindingPolicy,
    RelationTypeDefinition,
    RelationSemanticRuleDefinition,
    ScenarioOverrideDefinition,
    SemanticTagDefinition,
)


# Domain customization entry points: update these section blocks first when
# adapting the explainer to a concrete economy/finance application.
ECONOMY_SUPPORTED_INTENTS = [
    "causal_explanation",
    "containment_trace",
    "attribute_trace",
    "process_trace",
    "comparison_explanation",
    "provenance_trace",
]


ECONOMY_INTENT_BINDINGS = [
    IntentBindingDefinition(
        intent_id="economy_policy_impact",
        intent_family="causal_explanation",
        triggers=[
            "政策影响",
            "政策如何影响",
            "补贴影响",
            "监管影响",
            "policy impact",
            "regulation affect",
        ],
        negative_triggers=["属于行业", "公司简介"],
        preferred_semantic_tags=["policy", "impact", "metric"],
        preferred_relation_types=["policy_supports", "affects_metric"],
        evidence_policy_id="economy_causal_strict",
        output_contract_id="economy_causal_contract",
        template_id="economy_causal_v1",
    ),
    IntentBindingDefinition(
        intent_id="economy_metric_driver",
        intent_family="causal_explanation",
        triggers=[
            "利润为什么变化",
            "毛利率为何变化",
            "收入增长原因",
            "what drives revenue",
            "what drives margin",
        ],
        preferred_semantic_tags=["metric", "cost", "risk", "impact"],
        preferred_relation_types=["affects_metric"],
        scenario_id="economy_metric_driver_scenario_v1",
    ),
    IntentBindingDefinition(
        intent_id="economy_industry_membership",
        intent_family="containment_trace",
        triggers=[
            "属于什么行业",
            "所属行业",
            "产业链位置",
            "belongs to industry",
        ],
        negative_triggers=["为什么", "影响"],
        preferred_semantic_tags=["contain", "industry"],
        preferred_relation_types=["belongs_to_industry"],
        evidence_policy_id="structural_default",
        output_contract_id="structural_contract",
        template_id="economy_membership_v1",
    ),
]


ECONOMY_SEMANTIC_TAGS = [
    SemanticTagDefinition(
        tag_id="policy",
        description="policy and regulation semantics",
        aliases=["policy", "regulation", "subsid", "政策", "监管", "补贴"],
    ),
    SemanticTagDefinition(
        tag_id="cost",
        description="cost and margin semantics",
        aliases=["cost", "price", "margin", "expense", "成本", "价格", "利润", "毛利"],
    ),
    SemanticTagDefinition(
        tag_id="risk",
        description="market and operating risk semantics",
        aliases=["risk", "pressure", "uncertain", "风险", "压力", "波动"],
    ),
    SemanticTagDefinition(
        tag_id="metric",
        description="business metric semantics",
        aliases=["metric", "revenue", "growth", "收入", "增长", "指标"],
    ),
    SemanticTagDefinition(
        tag_id="industry",
        description="industry and sector semantics",
        aliases=["industry", "sector", "产业", "行业", "赛道"],
    ),
]


ECONOMY_RELATION_SEMANTICS = [
    RelationSemanticRuleDefinition(
        relation_type="policy_supports",
        aliases=["政策支持", "扶持", "刺激", "支持"],
        semantic_tags=["policy", "cause", "impact"],
        direction="source_to_target",
        allowed_intents=["causal_explanation"],
        source_types=["Policy", "Institution"],
        target_types=["Company", "Industry", "Asset", "Metric"],
        explanation_mode="causal_driver",
    ),
    RelationSemanticRuleDefinition(
        relation_type="affects_metric",
        aliases=["影响", "驱动", "压制", "改善"],
        semantic_tags=["metric", "cost", "risk", "impact"],
        direction="source_to_target",
        allowed_intents=["causal_explanation", "comparison_explanation"],
        source_types=["Event", "Policy", "Asset", "Institution", "Industry", "Company"],
        target_types=["Metric"],
        explanation_mode="metric_driver",
    ),
    RelationSemanticRuleDefinition(
        relation_type="belongs_to_industry",
        aliases=["所属行业", "属于", "布局于", "深耕"],
        semantic_tags=["contain", "industry"],
        direction="source_to_target",
        allowed_intents=["containment_trace", "attribute_trace"],
        source_types=["Company", "Institution"],
        target_types=["Industry"],
        explanation_mode="structural_membership",
    ),
    RelationSemanticRuleDefinition(
        relation_type="operates_in_country",
        aliases=["位于", "面向", "覆盖"],
        semantic_tags=["attribute"],
        direction="source_to_target",
        allowed_intents=["attribute_trace"],
        source_types=["Company", "Institution", "Industry"],
        target_types=["Country"],
        explanation_mode="attribute_mapping",
    ),
]


ECONOMY_NODE_ROLE_RULES = [
    NodeRoleRuleDefinition(
        node_type="Policy",
        roles_by_intent={
            "causal_explanation": ["driver", "constraint"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Event",
        roles_by_intent={
            "causal_explanation": ["driver", "intermediate_entity"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Company",
        roles_by_intent={
            "causal_explanation": ["subject_entity", "affected_entity"],
            "containment_trace": ["member_entity", "subject_entity"],
            "attribute_trace": ["subject_entity"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Metric",
        roles_by_intent={
            "causal_explanation": ["measured_target", "outcome"],
            "attribute_trace": ["attribute_target"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Industry",
        roles_by_intent={
            "causal_explanation": ["intermediate_entity", "industry_context"],
            "containment_trace": ["container_entity", "grouping_entity"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Country",
        roles_by_intent={
            "attribute_trace": ["attribute_value", "country_context"],
        },
    ),
]


ECONOMY_PATH_CONSTRAINTS = PathConstraintPolicy(
    default_max_hops=4,
    prefer_shorter_paths=True,
    penalize_repeated_nodes=True,
    require_direction_match_when_declared=True,
    disallow_unknown_node_roles=False,
)


ECONOMY_EVIDENCE_POLICIES = [
    EvidencePolicyDefinition(
        policy_id="economy_causal_strict",
        applies_to_intents=["causal_explanation"],
        require_evidence=True,
        require_per_hop_support=True,
        min_supporting_chunks=1,
        allow_partial_support=False,
        citation_granularity="path",
    ),
]


ECONOMY_OUTPUT_CONTRACTS = [
    OutputContractDefinition(
        contract_id="economy_causal_contract",
        applies_to_intents=["causal_explanation"],
        required_sections=["answer", "causal_chain", "evidence_summary", "uncertainty"],
        optional_sections=["metric_impact_note", "limits"],
        require_uncertainty_when_partial=True,
    ),
    OutputContractDefinition(
        contract_id="economy_metric_driver_contract",
        applies_to_intents=["causal_explanation"],
        required_sections=["answer", "causal_chain", "metric_impact_note", "evidence_summary", "uncertainty"],
        optional_sections=["limits"],
        require_uncertainty_when_partial=True,
    ),
]


ECONOMY_GUARDRAILS = GuardrailPolicy(
    disallow_unbacked_new_relations=True,
    require_explicit_uncertainty_on_partial_support=True,
    extra_flags={"disallow_metric_claim_without_support": True},
)


ECONOMY_SCENARIO_OVERRIDES = [
    ScenarioOverrideDefinition(
        scenario_id="economy_metric_driver_scenario_v1",
        description="Metric-driver explanation override for economy causal queries.",
        applies_to_intent="causal_explanation",
        template_id="economy_metric_driver_v1",
        evidence_policy_id="economy_causal_strict",
        output_contract_id="economy_metric_driver_contract",
        guardrails=GuardrailPolicy(
            require_explicit_uncertainty_on_partial_support=True,
            extra_flags={"disallow_metric_claim_without_support": True},
            extra_constraints=[
                "Prefer metric-driver wording over general macro narrative.",
                "Do not imply precise magnitude without explicit evidence.",
            ],
        ),
        metadata={"focus": "metric_driver"},
    ),
]


ECONOMY_PROMPT_BINDINGS = PromptBindingPolicy(
    default_template_id="path_base_v1",
    template_by_intent={
        "causal_explanation": "economy_causal_v1",
        "containment_trace": "economy_membership_v1",
        "attribute_trace": "economy_attribute_v1",
    },
    template_by_scenario={
        "economy_metric_driver_scenario_v1": "economy_metric_driver_v1",
    },
    extra_constraints=[
        "Prefer explicit driver to outcome wording when evidence supports it.",
        "Do not overstate quantitative impact if the evidence is qualitative only.",
    ],
)


ECONOMY_EXPLANATION_METADATA = {
    "scoring": {
        "prefer_paths_with_metric_targets": True,
        "penalize_generic_macro_nodes_when_specific_company_paths_exist": True,
    }
}


# Assemble the final profile from the section blocks above so field ownership
# stays obvious when customizing a domain template.
ECONOMY_EXPLANATION_PROFILE = ExplanationProfile(
    profile_id="economy_explainer",
    version="v1",
    extends="general_explainer",
    risk_level="medium",
    supported_intents=ECONOMY_SUPPORTED_INTENTS,
    intent_bindings=ECONOMY_INTENT_BINDINGS,
    semantic_tags=ECONOMY_SEMANTIC_TAGS,
    relation_semantics=ECONOMY_RELATION_SEMANTICS,
    node_role_rules=ECONOMY_NODE_ROLE_RULES,
    path_constraints=ECONOMY_PATH_CONSTRAINTS,
    evidence_policies=ECONOMY_EVIDENCE_POLICIES,
    output_contracts=ECONOMY_OUTPUT_CONTRACTS,
    guardrails=ECONOMY_GUARDRAILS,
    scenario_overrides=ECONOMY_SCENARIO_OVERRIDES,
    prompt_bindings=ECONOMY_PROMPT_BINDINGS,
    metadata=ECONOMY_EXPLANATION_METADATA,
)


ECONOMY_DOMAIN_SCHEMA = DomainSchema(
    domain_name="economy",
    profile_name="economy",
    enabled=True,
    mode="domain",
    language=DEFAULT_SUMMARY_LANGUAGE,
    description="Economy and finance oriented schema profile for extracting companies, policies, metrics, industries, and market events.",
    entity_types=[
        EntityTypeDefinition(
            name="Company",
            display_name="公司",
            description="Listed companies, private firms, issuers, and named enterprises.",
            aliases=["企业", "上市公司", "公司主体"],
        ),
        EntityTypeDefinition(
            name="Industry",
            display_name="行业",
            description="Sectors, subsectors, industrial chains, and market segments.",
            aliases=["产业", "赛道", "板块"],
        ),
        EntityTypeDefinition(
            name="Metric",
            display_name="指标",
            description="Financial and operating indicators such as revenue, margin, cash flow, valuation, and output.",
            aliases=["财务指标", "经营指标", "数据指标"],
        ),
        EntityTypeDefinition(
            name="Policy",
            display_name="政策",
            description="Fiscal, monetary, industrial, regulatory, and subsidy policies.",
            aliases=["政策工具", "监管政策", "产业政策"],
        ),
        EntityTypeDefinition(
            name="Event",
            display_name="事件",
            description="Macro events, market events, announcements, and time-bounded developments.",
            aliases=["市场事件", "公告事件", "经营事件"],
        ),
        EntityTypeDefinition(
            name="Asset",
            display_name="资产",
            description="Commodities, securities, currencies, bonds, and other tradable or reference assets.",
            aliases=["商品", "证券", "金融资产"],
        ),
        EntityTypeDefinition(
            name="Institution",
            display_name="机构",
            description="Banks, regulators, exchanges, funds, agencies, and policy institutions.",
            aliases=["金融机构", "监管机构", "政府机构"],
        ),
        EntityTypeDefinition(
            name="Country",
            display_name="国家",
            description="Countries, sovereign regions, and national economies.",
            aliases=["地区", "经济体"],
        ),
    ],
    relation_types=[
        RelationTypeDefinition(
            name="policy_supports",
            display_name="政策支持",
            description="A policy or institution supports a company, industry, asset, or economic activity.",
            source_types=["Policy", "Institution"],
            target_types=["Company", "Industry", "Asset", "Metric"],
            aliases=["扶持", "刺激", "支持"],
        ),
        RelationTypeDefinition(
            name="affects_metric",
            display_name="影响指标",
            description="An event, policy, asset, or institution changes a financial or operational metric.",
            source_types=["Event", "Policy", "Asset", "Institution"],
            target_types=["Metric"],
            aliases=["影响", "驱动", "压制", "改善"],
        ),
        RelationTypeDefinition(
            name="belongs_to_industry",
            display_name="所属行业",
            description="A company or institution belongs to or primarily operates in an industry.",
            source_types=["Company", "Institution"],
            target_types=["Industry"],
            aliases=["属于", "布局于", "深耕"],
        ),
        RelationTypeDefinition(
            name="operates_in_country",
            display_name="所在国家",
            description="A company, institution, or industry activity operates in or is associated with a country.",
            source_types=["Company", "Institution", "Industry"],
            target_types=["Country"],
            aliases=["位于", "面向", "覆盖"],
        ),
    ],
    explanation_profile=ECONOMY_EXPLANATION_PROFILE,
    aliases={
        "公司": "Company",
        "行业": "Industry",
        "指标": "Metric",
        "政策": "Policy",
        "事件": "Event",
        "资产": "Asset",
        "机构": "Institution",
        "国家": "Country",
    },
    extraction_rules=[
        "Prefer economically meaningful entities over generic nouns.",
        "When the text does not provide a proper company name, allow a normalized descriptive placeholder such as 'A battery materials company'.",
        "Prefer explicit policy, institution, metric, and industry relations when they are directly supported by the text.",
    ],
    metadata={"builtin": True, "domain": "economy"},
)
