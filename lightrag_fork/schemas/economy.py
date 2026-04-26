from __future__ import annotations

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
        aliases=["政策支持", "政策扶持", "政策刺激"],
        semantic_tags=["policy", "cause", "impact"],
        direction="source_to_target",
        allowed_intents=["causal_explanation"],
        source_types=["Policy", "Institution"],
        target_types=["Company", "Industry", "Asset", "Metric"],
        explanation_mode="causal_driver",
    ),
    RelationSemanticRuleDefinition(
        relation_type="affects_metric",
        aliases=["影响指标", "驱动指标", "压制指标", "改善指标"],
        semantic_tags=["metric", "cost", "risk", "impact"],
        direction="source_to_target",
        allowed_intents=["causal_explanation", "comparison_explanation"],
        source_types=["Event", "Policy", "Asset", "Institution", "Industry", "Company"],
        target_types=["Metric"],
        explanation_mode="metric_driver",
    ),
    RelationSemanticRuleDefinition(
        relation_type="belongs_to_industry",
        aliases=["所属行业", "行业归属"],
        semantic_tags=["contain", "industry"],
        direction="source_to_target",
        allowed_intents=["containment_trace", "attribute_trace"],
        source_types=["Company", "Institution"],
        target_types=["Industry"],
        explanation_mode="structural_membership",
    ),
    RelationSemanticRuleDefinition(
        relation_type="operates_in_location",
        aliases=["所在地区", "所在国家", "所在市场"],
        semantic_tags=["attribute"],
        direction="source_to_target",
        allowed_intents=["attribute_trace"],
        source_types=["Company", "Institution", "Industry"],
        target_types=["Location"],
        explanation_mode="attribute_mapping",
    ),
    RelationSemanticRuleDefinition(
        relation_type="founded_by",
        aliases=["创始人", "联合创始人"],
        semantic_tags=["attribute"],
        direction="source_to_target",
        allowed_intents=["attribute_trace", "provenance_trace"],
        source_types=["Company", "Institution"],
        target_types=["Person"],
        explanation_mode="attribute_mapping",
    ),
    RelationSemanticRuleDefinition(
        relation_type="led_by",
        aliases=["管理者", "负责人", "董事长", "首席执行官"],
        semantic_tags=["attribute"],
        direction="source_to_target",
        allowed_intents=["attribute_trace"],
        source_types=["Company", "Institution"],
        target_types=["Person"],
        explanation_mode="attribute_mapping",
    ),
    RelationSemanticRuleDefinition(
        relation_type="invests_in",
        aliases=["投资于", "持股于", "参股于"],
        semantic_tags=["attribute", "impact"],
        direction="source_to_target",
        allowed_intents=["attribute_trace", "comparison_explanation"],
        source_types=["Person", "Company", "Institution", "Asset"],
        target_types=["Company", "Asset", "Industry"],
        explanation_mode="attribute_mapping",
    ),
    RelationSemanticRuleDefinition(
        relation_type="associated_with_concept",
        aliases=["关联概念", "核心理念", "投资理念", "主题概念"],
        semantic_tags=["attribute"],
        direction="source_to_target",
        allowed_intents=["attribute_trace", "comparison_explanation"],
        source_types=["Person", "Company", "Institution", "Policy", "Event", "Industry", "Asset"],
        target_types=["Concept"],
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
        node_type="Location",
        roles_by_intent={
            "attribute_trace": ["attribute_value", "location_context"],
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
    language="Chinese",
    description="Economy and finance oriented schema profile for extracting companies, people, concepts, policies, metrics, industries, locations, and market events.",
    entity_types=[
        EntityTypeDefinition(
            name="Company",
            display_name="公司",
            description="Listed companies, private firms, issuers, and named enterprises.",
            aliases=[
                "企业",
                "上市公司",
                "公司主体",
                "组织",
                "机构主体",
                "organization",
                "organisation",
                "firm",
                "issuer",
                "corporation",
                "listedcompany",
                "business",
                "enterprise",
            ],
        ),
        EntityTypeDefinition(
            name="Industry",
            display_name="行业",
            description="Sectors, subsectors, industrial chains, and market segments.",
            aliases=[
                "产业",
                "产业链",
                "赛道",
                "板块",
                "细分行业",
                "industry",
                "sector",
                "subsector",
                "segment",
            ],
        ),
        EntityTypeDefinition(
            name="Metric",
            display_name="指标",
            description="Financial and operating indicators such as revenue, margin, cash flow, valuation, and output.",
            aliases=[
                "财务指标",
                "经营指标",
                "数据指标",
                "经济指标",
                "估值指标",
                "metric",
                "indicator",
                "index",
                "revenue",
                "margin",
                "cashflow",
                "valuation",
            ],
        ),
        EntityTypeDefinition(
            name="Policy",
            display_name="政策",
            description="Fiscal, monetary, industrial, regulatory, and subsidy policies.",
            aliases=[
                "政策工具",
                "监管政策",
                "产业政策",
                "财政政策",
                "货币政策",
                "补贴政策",
                "监管",
                "补贴",
                "policy",
                "regulation",
                "regulatorypolicy",
            ],
        ),
        EntityTypeDefinition(
            name="Event",
            display_name="事件",
            description="Macro events, market events, announcements, and time-bounded developments.",
            aliases=[
                "市场事件",
                "公告事件",
                "经营事件",
                "宏观事件",
                "新闻事件",
                "event",
                "announcement",
                "news",
                "development",
            ],
        ),
        EntityTypeDefinition(
            name="Asset",
            display_name="资产",
            description="Commodities, securities, currencies, bonds, and other tradable or reference assets.",
            aliases=[
                "商品",
                "证券",
                "金融资产",
                "股票",
                "债券",
                "基金",
                "期货",
                "汇率",
                "货币",
                "asset",
                "commodity",
                "security",
                "stock",
                "bond",
                "currency",
            ],
        ),
        EntityTypeDefinition(
            name="Institution",
            display_name="机构",
            description="Banks, regulators, exchanges, funds, agencies, and policy institutions.",
            aliases=[
                "金融机构",
                "监管机构",
                "政府机构",
                "央行",
                "交易所",
                "基金公司",
                "institution",
                "regulator",
                "agency",
                "bank",
                "fund",
                "exchange",
            ],
        ),
        EntityTypeDefinition(
            name="Location",
            display_name="地区",
            description="Countries, regions, markets, geographies, and named economic locations.",
            aliases=[
                "国家",
                "地区",
                "区域",
                "市场",
                "经济体",
                "地理区域",
                "location",
                "country",
                "region",
                "market",
                "geography",
            ],
        ),
        EntityTypeDefinition(
            name="Person",
            display_name="人物",
            description="Named people such as executives, officials, analysts, economists, and investors.",
            aliases=[
                "人",
                "人物",
                "个人",
                "高管",
                "官员",
                "分析师",
                "经济学家",
                "投资者",
                "person",
                "people",
                "executive",
                "official",
                "analyst",
                "economist",
                "investor",
            ],
        ),
        EntityTypeDefinition(
            name="Concept",
            display_name="概念",
            description="Economy or finance concepts, themes, mechanisms, factors, and analytical abstractions.",
            aliases=[
                "概念",
                "主题",
                "机制",
                "因素",
                "风险因素",
                "逻辑",
                "框架",
                "concept",
                "theme",
                "topic",
                "factor",
                "mechanism",
                "riskfactor",
            ],
        ),
    ],
    relation_types=[
        RelationTypeDefinition(
            name="policy_supports",
            display_name="政策支持",
            description="A policy or institution supports a company, industry, asset, or economic activity.",
            source_types=["Policy", "Institution"],
            target_types=["Company", "Industry", "Asset", "Metric"],
            aliases=["政策扶持", "政策刺激"],
        ),
        RelationTypeDefinition(
            name="affects_metric",
            display_name="影响指标",
            description="An event, policy, asset, or institution changes a financial or operational metric.",
            source_types=["Event", "Policy", "Asset", "Institution"],
            target_types=["Metric"],
            aliases=["驱动指标", "压制指标", "改善指标"],
        ),
        RelationTypeDefinition(
            name="belongs_to_industry",
            display_name="所属行业",
            description="A company or institution belongs to or primarily operates in an industry.",
            source_types=["Company", "Institution"],
            target_types=["Industry"],
            aliases=["行业归属"],
        ),
        RelationTypeDefinition(
            name="operates_in_location",
            display_name="所在地区",
            description="A company, institution, or industry activity operates in or is associated with a location, country, region, or market.",
            source_types=["Company", "Institution", "Industry"],
            target_types=["Location"],
            aliases=["所在国家", "所在市场"],
        ),
        RelationTypeDefinition(
            name="founded_by",
            display_name="创始人",
            description="A company or institution was founded by a person.",
            source_types=["Company", "Institution"],
            target_types=["Person"],
            aliases=["联合创始人"],
        ),
        RelationTypeDefinition(
            name="led_by",
            display_name="管理者",
            description="A company or institution is led or managed by a person.",
            source_types=["Company", "Institution"],
            target_types=["Person"],
            aliases=["负责人", "董事长", "首席执行官"],
        ),
        RelationTypeDefinition(
            name="invests_in",
            display_name="投资于",
            description="A person, company, institution, or asset invests in another company, asset, or industry.",
            source_types=["Person", "Company", "Institution", "Asset"],
            target_types=["Company", "Asset", "Industry"],
            aliases=["持股于", "参股于"],
        ),
        RelationTypeDefinition(
            name="associated_with_concept",
            display_name="关联概念",
            description="A person, company, institution, policy, event, industry, or asset is associated with a concept or investment theme.",
            source_types=["Person", "Company", "Institution", "Policy", "Event", "Industry", "Asset"],
            target_types=["Concept"],
            aliases=["核心理念", "投资理念", "主题概念"],
        ),
    ],
    explanation_profile=ECONOMY_EXPLANATION_PROFILE,
    aliases={
        "公司": "Company",
        "企业": "Company",
        "上市公司": "Company",
        "组织": "Company",
        "organization": "Company",
        "firm": "Company",
        "行业": "Industry",
        "产业": "Industry",
        "板块": "Industry",
        "sector": "Industry",
        "指标": "Metric",
        "数据": "Metric",
        "indicator": "Metric",
        "政策": "Policy",
        "监管": "Policy",
        "事件": "Event",
        "新闻": "Event",
        "资产": "Asset",
        "商品": "Asset",
        "机构": "Institution",
        "国家": "Location",
        "地区": "Location",
        "区域": "Location",
        "location": "Location",
        "country": "Location",
        "region": "Location",
        "人物": "Person",
        "个人": "Person",
        "person": "Person",
        "概念": "Concept",
        "主题": "Concept",
        "concept": "Concept",
    },
    extraction_rules=[
        "Prefer economically meaningful entities over generic nouns.",
        "When the text does not provide a proper company name, allow a normalized descriptive placeholder such as 'A battery materials company'.",
        "Use only schema entity type names for entity_type. If a type is ambiguous or outside the schema, map it to the closest schema type and prefer Concept as the fallback.",
        "Prefer explicit policy, institution, metric, and industry relations when they are directly supported by the text.",
    ],
    metadata={
        "builtin": True,
        "domain": "economy",
        "entity_type_canonicalization": "strict",
        "strict_entity_types": True,
        "fallback_entity_type": "Concept",
    },
)
