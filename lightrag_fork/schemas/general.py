from __future__ import annotations

from lightrag_fork.constants import DEFAULT_ENTITY_TYPES, DEFAULT_SUMMARY_LANGUAGE
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
    RelationSemanticRuleDefinition,
    SemanticTagDefinition,
)


# Domain customization entry points: update these section blocks first when
# adapting the generic explanation contract to a concrete domain profile.
GENERAL_SUPPORTED_INTENTS = [
    "causal_explanation",
    "containment_trace",
    "prerequisite_trace",
    "attribute_trace",
    "process_trace",
    "provenance_trace",
    "responsibility_trace",
    "comparison_explanation",
]


GENERAL_INTENT_BINDINGS = [
    IntentBindingDefinition(
        intent_id="why_causal",
        intent_family="causal_explanation",
        triggers=["why", "how", "cause", "affect", "影响", "导致", "为什么", "原因"],
        negative_triggers=["属于", "包含", "前置"],
        preferred_semantic_tags=["cause", "impact"],
        template_id="intent_causal_v1",
    ),
    IntentBindingDefinition(
        intent_id="containment_lookup",
        intent_family="containment_trace",
        triggers=["属于", "包含", "隶属", "上位", "下位", "part of", "belong to"],
        negative_triggers=["为什么", "影响"],
        preferred_semantic_tags=["contain"],
        template_id="intent_containment_v1",
    ),
    IntentBindingDefinition(
        intent_id="prerequisite_lookup",
        intent_family="prerequisite_trace",
        triggers=["前置", "依赖", "先学", "prerequisite", "depend on"],
        preferred_semantic_tags=["prerequisite"],
        template_id="intent_prerequisite_v1",
    ),
    IntentBindingDefinition(
        intent_id="provenance_lookup",
        intent_family="provenance_trace",
        triggers=[
            "来源",
            "依据",
            "文档",
            "版本",
            "审批",
            "source",
            "document",
            "version",
        ],
        preferred_semantic_tags=["provenance", "responsibility"],
        template_id="intent_provenance_v1",
    ),
]


GENERAL_SEMANTIC_TAGS = [
    SemanticTagDefinition(
        tag_id="cause",
        description="causal driver semantics",
        aliases=["why", "cause", "reason", "导致", "原因", "为何", "为什么", "传导"],
    ),
    SemanticTagDefinition(
        tag_id="impact",
        description="effect and impact semantics",
        aliases=["impact", "affect", "effect", "drive", "推动", "带动", "影响", "拉动"],
    ),
    SemanticTagDefinition(
        tag_id="increase",
        description="increase or growth semantics",
        aliases=["increase", "growth", "rise", "surge", "增长", "提升", "扩张", "改善"],
    ),
    SemanticTagDefinition(
        tag_id="decrease",
        description="decrease or decline semantics",
        aliases=["decrease", "decline", "drop", "fall", "压缩", "下降", "减少", "下滑"],
    ),
    SemanticTagDefinition(
        tag_id="contain",
        description="containment and hierarchy semantics",
        aliases=["contain", "include", "属于", "包含", "隶属", "part of"],
    ),
    SemanticTagDefinition(
        tag_id="prerequisite",
        description="prerequisite and dependency semantics",
        aliases=["prerequisite", "dependency", "依赖", "前置", "先学"],
    ),
    SemanticTagDefinition(
        tag_id="attribute",
        description="attribute and property semantics",
        aliases=["attribute", "property", "属性", "特征"],
    ),
    SemanticTagDefinition(
        tag_id="process",
        description="process and workflow semantics",
        aliases=["process", "workflow", "mechanism", "流程", "机制"],
    ),
    SemanticTagDefinition(
        tag_id="provenance",
        description="source and provenance semantics",
        aliases=["source", "document", "version", "来源", "文档", "版本"],
    ),
    SemanticTagDefinition(
        tag_id="responsibility",
        description="ownership and responsibility semantics",
        aliases=["owner", "responsible", "approver", "归属", "负责人", "审批"],
    ),
]


GENERAL_RELATION_SEMANTICS = [
    RelationSemanticRuleDefinition(
        relation_type="affects",
        aliases=["impact", "affect", "effect", "影响", "推动", "带动"],
        semantic_tags=["cause", "impact"],
        direction="source_to_target",
        allowed_intents=["causal_explanation"],
        explanation_mode="causal_driver",
    ),
    RelationSemanticRuleDefinition(
        relation_type="contains",
        aliases=["contain", "include", "属于", "包含", "隶属", "part of"],
        semantic_tags=["contain"],
        direction="source_to_target",
        allowed_intents=["containment_trace"],
        explanation_mode="structural_membership",
    ),
    RelationSemanticRuleDefinition(
        relation_type="prerequisite_of",
        aliases=["prerequisite", "dependency", "依赖", "前置"],
        semantic_tags=["prerequisite"],
        direction="source_to_target",
        allowed_intents=["prerequisite_trace"],
        explanation_mode="ordered_dependency",
    ),
    RelationSemanticRuleDefinition(
        relation_type="documented_in",
        aliases=["source", "document", "version", "来源", "文档", "版本"],
        semantic_tags=["provenance", "responsibility"],
        direction="source_to_target",
        allowed_intents=["provenance_trace", "responsibility_trace"],
        explanation_mode="source_trace",
    ),
]


GENERAL_NODE_ROLE_RULES = [
    NodeRoleRuleDefinition(
        node_type="Event",
        roles_by_intent={
            "causal_explanation": ["driver", "intermediate_entity"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Organization",
        roles_by_intent={
            "causal_explanation": ["subject_entity", "affected_entity"],
            "responsibility_trace": ["responsible_party"],
            "containment_trace": ["member_entity", "container_entity"],
        },
    ),
    NodeRoleRuleDefinition(
        node_type="Location",
        roles_by_intent={
            "attribute_trace": ["attribute_value"],
        },
    ),
]


GENERAL_PATH_CONSTRAINTS = PathConstraintPolicy(
    default_max_hops=4,
    prefer_shorter_paths=True,
    penalize_repeated_nodes=True,
    require_direction_match_when_declared=True,
    disallow_unknown_node_roles=False,
)


GENERAL_EVIDENCE_POLICIES = [
    EvidencePolicyDefinition(
        policy_id="causal_default",
        applies_to_intents=["causal_explanation"],
        require_evidence=True,
        require_per_hop_support=True,
        min_supporting_chunks=1,
        allow_partial_support=False,
        citation_granularity="path",
    ),
    EvidencePolicyDefinition(
        policy_id="structural_default",
        applies_to_intents=[
            "containment_trace",
            "prerequisite_trace",
            "attribute_trace",
            "process_trace",
        ],
        require_evidence=True,
        require_per_hop_support=False,
        min_supporting_chunks=1,
        allow_partial_support=True,
        citation_granularity="chunk",
    ),
    EvidencePolicyDefinition(
        policy_id="provenance_default",
        applies_to_intents=["provenance_trace", "responsibility_trace"],
        require_evidence=True,
        require_per_hop_support=True,
        min_supporting_chunks=1,
        allow_partial_support=False,
        citation_granularity="document",
    ),
]


GENERAL_OUTPUT_CONTRACTS = [
    OutputContractDefinition(
        contract_id="causal_contract",
        applies_to_intents=["causal_explanation"],
        required_sections=["answer", "causal_chain", "evidence_summary", "uncertainty"],
        optional_sections=["limits"],
        require_uncertainty_when_partial=True,
    ),
    OutputContractDefinition(
        contract_id="structural_contract",
        applies_to_intents=[
            "containment_trace",
            "prerequisite_trace",
            "attribute_trace",
            "process_trace",
        ],
        required_sections=["answer", "path_summary", "evidence_summary"],
        optional_sections=["uncertainty"],
        require_uncertainty_when_partial=True,
    ),
    OutputContractDefinition(
        contract_id="provenance_contract",
        applies_to_intents=["provenance_trace", "responsibility_trace"],
        required_sections=["answer", "source_chain", "evidence_summary", "uncertainty"],
        optional_sections=["version_notes"],
        require_uncertainty_when_partial=True,
    ),
]


GENERAL_GUARDRAILS = GuardrailPolicy(
    disallow_unbacked_new_relations=True,
    disallow_cross_path_fabrication=True,
    require_explicit_uncertainty_on_partial_support=True,
)


GENERAL_PROMPT_BINDINGS = PromptBindingPolicy(
    default_template_id="path_base_v1",
    template_by_intent={
        "causal_explanation": "intent_causal_v1",
        "containment_trace": "intent_containment_v1",
        "prerequisite_trace": "intent_prerequisite_v1",
        "provenance_trace": "intent_provenance_v1",
    },
)


GENERAL_EXPLANATION_METADATA = {
    "scoring": {
        "default_max_hops": 4,
        "prefer_shorter_paths": True,
        "penalize_repeated_nodes": True,
    }
}


# Assemble the final profile from the section blocks above so field ownership
# stays obvious when customizing a domain template.
GENERAL_EXPLANATION_PROFILE = ExplanationProfile(
    profile_id="general_explainer",
    version="v1",
    risk_level="low",
    supported_intents=GENERAL_SUPPORTED_INTENTS,
    intent_bindings=GENERAL_INTENT_BINDINGS,
    semantic_tags=GENERAL_SEMANTIC_TAGS,
    relation_semantics=GENERAL_RELATION_SEMANTICS,
    node_role_rules=GENERAL_NODE_ROLE_RULES,
    path_constraints=GENERAL_PATH_CONSTRAINTS,
    evidence_policies=GENERAL_EVIDENCE_POLICIES,
    output_contracts=GENERAL_OUTPUT_CONTRACTS,
    guardrails=GENERAL_GUARDRAILS,
    prompt_bindings=GENERAL_PROMPT_BINDINGS,
    metadata=GENERAL_EXPLANATION_METADATA,
)


GENERAL_DOMAIN_SCHEMA = DomainSchema(
    domain_name="general",
    profile_name="general",
    enabled=False,
    mode="general",
    language=DEFAULT_SUMMARY_LANGUAGE,
    description="Default general-purpose schema that preserves the original LightRAG extraction behavior.",
    entity_types=[
        EntityTypeDefinition(
            name=entity_type,
            display_name=entity_type,
            description="Generic entity category inherited from the original LightRAG defaults.",
        )
        for entity_type in DEFAULT_ENTITY_TYPES
    ],
    relation_types=[],
    explanation_profile=GENERAL_EXPLANATION_PROFILE,
    aliases={},
    extraction_rules=[],
    metadata={"builtin": True},
)
