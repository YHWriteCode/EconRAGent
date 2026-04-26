from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping

from lightrag_fork.constants import (
    DEFAULT_DOMAIN_SCHEMA_ENABLED,
    DEFAULT_DOMAIN_SCHEMA_MODE,
    DEFAULT_DOMAIN_SCHEMA_PROFILE,
    DEFAULT_ENTITY_TYPES,
    DEFAULT_SUMMARY_LANGUAGE,
)


@dataclass(frozen=True)
class EntityTypeDefinition:
    name: str
    display_name: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RelationTypeDefinition:
    name: str
    display_name: str = ""
    description: str = ""
    source_types: list[str] = field(default_factory=list)
    target_types: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SemanticTagDefinition:
    tag_id: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IntentBindingDefinition:
    intent_id: str
    intent_family: str
    triggers: list[str] = field(default_factory=list)
    negative_triggers: list[str] = field(default_factory=list)
    preferred_semantic_tags: list[str] = field(default_factory=list)
    preferred_relation_types: list[str] = field(default_factory=list)
    evidence_policy_id: str | None = None
    output_contract_id: str | None = None
    template_id: str | None = None
    scenario_id: str | None = None


@dataclass(frozen=True)
class RelationSemanticRuleDefinition:
    relation_type: str
    aliases: list[str] = field(default_factory=list)
    semantic_tags: list[str] = field(default_factory=list)
    direction: str = "undirected"
    allowed_intents: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    target_types: list[str] = field(default_factory=list)
    explanation_mode: str | None = None


@dataclass(frozen=True)
class EvidencePolicyDefinition:
    policy_id: str
    applies_to_intents: list[str] = field(default_factory=list)
    require_evidence: bool = True
    require_per_hop_support: bool = False
    min_supporting_chunks: int = 1
    allow_partial_support: bool = True
    require_counter_evidence: bool = False
    citation_granularity: str = "chunk"


@dataclass(frozen=True)
class OutputContractDefinition:
    contract_id: str
    applies_to_intents: list[str] = field(default_factory=list)
    required_sections: list[str] = field(default_factory=list)
    optional_sections: list[str] = field(default_factory=list)
    require_uncertainty_when_partial: bool = True


@dataclass(frozen=True)
class NodeRoleRuleDefinition:
    node_type: str
    roles_by_intent: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PathConstraintPolicy:
    default_max_hops: int | None = None
    prefer_shorter_paths: bool | None = None
    penalize_repeated_nodes: bool | None = None
    require_direction_match_when_declared: bool | None = None
    disallow_unknown_node_roles: bool | None = None


@dataclass(frozen=True)
class GuardrailPolicy:
    disallow_unbacked_new_relations: bool = False
    disallow_cross_path_fabrication: bool = False
    require_explicit_uncertainty_on_partial_support: bool = False
    extra_flags: dict[str, bool] = field(default_factory=dict)
    extra_constraints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioOverrideDefinition:
    scenario_id: str
    description: str = ""
    applies_to_intent: str = ""
    template_id: str | None = None
    evidence_policy_id: str | None = None
    output_contract_id: str | None = None
    guardrails: GuardrailPolicy = field(default_factory=GuardrailPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptBindingPolicy:
    default_template_id: str = ""
    template_by_intent: dict[str, str] = field(default_factory=dict)
    template_by_scenario: dict[str, str] = field(default_factory=dict)
    extra_constraints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExplanationProfile:
    profile_id: str
    version: str = "v1"
    extends: str | None = None
    risk_level: str = "low"
    supported_intents: list[str] = field(default_factory=list)
    intent_bindings: list[IntentBindingDefinition] = field(default_factory=list)
    semantic_tags: list[SemanticTagDefinition] = field(default_factory=list)
    relation_semantics: list[RelationSemanticRuleDefinition] = field(default_factory=list)
    node_role_rules: list[NodeRoleRuleDefinition] = field(default_factory=list)
    path_constraints: PathConstraintPolicy = field(default_factory=PathConstraintPolicy)
    evidence_policies: list[EvidencePolicyDefinition] = field(default_factory=list)
    output_contracts: list[OutputContractDefinition] = field(default_factory=list)
    guardrails: GuardrailPolicy = field(default_factory=GuardrailPolicy)
    scenario_overrides: list[ScenarioOverrideDefinition] = field(default_factory=list)
    prompt_bindings: PromptBindingPolicy = field(default_factory=PromptBindingPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_runtime_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "extends": self.extends,
            "risk_level": self.risk_level,
            "supported_intents": list(self.supported_intents),
            "intent_bindings": [asdict(item) for item in self.intent_bindings],
            "semantic_tags": [asdict(item) for item in self.semantic_tags],
            "relation_semantics": [asdict(item) for item in self.relation_semantics],
            "node_role_rules": [asdict(item) for item in self.node_role_rules],
            "path_constraints": asdict(self.path_constraints),
            "evidence_policies": [asdict(item) for item in self.evidence_policies],
            "output_contracts": [asdict(item) for item in self.output_contracts],
            "guardrails": asdict(self.guardrails),
            "scenario_overrides": [asdict(item) for item in self.scenario_overrides],
            "prompt_bindings": asdict(self.prompt_bindings),
            "metadata": deepcopy(self.metadata),
        }


@dataclass(frozen=True)
class DomainSchema:
    domain_name: str
    profile_name: str
    enabled: bool = DEFAULT_DOMAIN_SCHEMA_ENABLED
    mode: str = DEFAULT_DOMAIN_SCHEMA_MODE
    language: str = DEFAULT_SUMMARY_LANGUAGE
    description: str = ""
    entity_types: list[EntityTypeDefinition] = field(default_factory=list)
    relation_types: list[RelationTypeDefinition] = field(default_factory=list)
    explanation_profile: ExplanationProfile | None = None
    aliases: dict[str, str | list[str]] = field(default_factory=dict)
    extraction_rules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_runtime_dict(self) -> dict[str, Any]:
        entity_type_names = [item.name for item in self.entity_types]
        relation_type_names = [item.name for item in self.relation_types]
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "profile_name": self.profile_name,
            "domain_name": self.domain_name,
            "language": self.language,
            "description": self.description,
            "entity_types": [asdict(item) for item in self.entity_types],
            "entity_type_names": entity_type_names,
            "relation_types": [asdict(item) for item in self.relation_types],
            "relation_type_names": relation_type_names,
            "explanation_profile": (
                self.explanation_profile.to_runtime_dict()
                if self.explanation_profile is not None
                else None
            ),
            "aliases": deepcopy(self.aliases),
            "extraction_rules": list(self.extraction_rules),
            "metadata": deepcopy(self.metadata),
        }


def _normalize_schema_match_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.strip().lower() if char.isalnum())


def _coerce_string_list(items: Iterable[Any] | None) -> list[str]:
    return [str(item).strip() for item in (items or []) if str(item).strip()]


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coerce_positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _merge_unique_strings(*groups: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            merged.append(normalized)
            seen.add(normalized)
    return merged


def _deep_merge_mappings(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            isinstance(merged.get(key), Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = _deep_merge_mappings(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _coerce_entity_type(item: Any) -> EntityTypeDefinition:
    if isinstance(item, EntityTypeDefinition):
        return item
    if isinstance(item, str):
        return EntityTypeDefinition(name=item, display_name=item)
    if isinstance(item, Mapping):
        return EntityTypeDefinition(
            name=str(item.get("name") or item.get("display_name") or "").strip(),
            display_name=str(item.get("display_name") or item.get("name") or "").strip(),
            description=str(item.get("description") or "").strip(),
            aliases=[str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()],
        )
    raise TypeError(f"Unsupported entity type definition: {type(item)!r}")


def _coerce_relation_type(item: Any) -> RelationTypeDefinition:
    if isinstance(item, RelationTypeDefinition):
        return item
    if isinstance(item, str):
        return RelationTypeDefinition(name=item, display_name=item)
    if isinstance(item, Mapping):
        return RelationTypeDefinition(
            name=str(item.get("name") or item.get("display_name") or "").strip(),
            display_name=str(item.get("display_name") or item.get("name") or "").strip(),
            description=str(item.get("description") or "").strip(),
            source_types=[str(value).strip() for value in item.get("source_types", []) if str(value).strip()],
            target_types=[str(value).strip() for value in item.get("target_types", []) if str(value).strip()],
            aliases=[str(alias).strip() for alias in item.get("aliases", []) if str(alias).strip()],
        )
    raise TypeError(f"Unsupported relation type definition: {type(item)!r}")


def _coerce_semantic_tag(item: Any) -> SemanticTagDefinition:
    if isinstance(item, SemanticTagDefinition):
        return item
    if isinstance(item, str):
        normalized = str(item).strip()
        return SemanticTagDefinition(tag_id=normalized, description=normalized)
    if isinstance(item, Mapping):
        return SemanticTagDefinition(
            tag_id=str(item.get("tag_id") or item.get("name") or "").strip(),
            description=str(item.get("description") or "").strip(),
            aliases=_coerce_string_list(item.get("aliases")),
        )
    raise TypeError(f"Unsupported semantic tag definition: {type(item)!r}")


def _coerce_intent_binding(item: Any) -> IntentBindingDefinition:
    if isinstance(item, IntentBindingDefinition):
        return item
    if isinstance(item, Mapping):
        return IntentBindingDefinition(
            intent_id=str(item.get("intent_id") or "").strip(),
            intent_family=str(item.get("intent_family") or "").strip(),
            triggers=_coerce_string_list(item.get("triggers")),
            negative_triggers=_coerce_string_list(item.get("negative_triggers")),
            preferred_semantic_tags=_coerce_string_list(
                item.get("preferred_semantic_tags")
            ),
            preferred_relation_types=_coerce_string_list(
                item.get("preferred_relation_types")
            ),
            evidence_policy_id=str(item.get("evidence_policy_id") or "").strip() or None,
            output_contract_id=str(item.get("output_contract_id") or "").strip() or None,
            template_id=str(item.get("template_id") or "").strip() or None,
            scenario_id=str(item.get("scenario_id") or "").strip() or None,
        )
    raise TypeError(f"Unsupported intent binding definition: {type(item)!r}")


def _coerce_relation_semantic_rule(item: Any) -> RelationSemanticRuleDefinition:
    if isinstance(item, RelationSemanticRuleDefinition):
        return item
    if isinstance(item, Mapping):
        return RelationSemanticRuleDefinition(
            relation_type=str(item.get("relation_type") or "").strip(),
            aliases=_coerce_string_list(item.get("aliases")),
            semantic_tags=_coerce_string_list(item.get("semantic_tags")),
            direction=str(item.get("direction") or "undirected").strip() or "undirected",
            allowed_intents=_coerce_string_list(item.get("allowed_intents")),
            source_types=_coerce_string_list(item.get("source_types")),
            target_types=_coerce_string_list(item.get("target_types")),
            explanation_mode=str(item.get("explanation_mode") or "").strip() or None,
        )
    raise TypeError(f"Unsupported relation semantic definition: {type(item)!r}")


def _coerce_evidence_policy(item: Any) -> EvidencePolicyDefinition:
    if isinstance(item, EvidencePolicyDefinition):
        return item
    if isinstance(item, Mapping):
        return EvidencePolicyDefinition(
            policy_id=str(item.get("policy_id") or "").strip(),
            applies_to_intents=_coerce_string_list(item.get("applies_to_intents")),
            require_evidence=_coerce_bool(item.get("require_evidence"), True),
            require_per_hop_support=_coerce_bool(
                item.get("require_per_hop_support"),
                False,
            ),
            min_supporting_chunks=_coerce_positive_int(
                item.get("min_supporting_chunks"),
                1,
            ),
            allow_partial_support=_coerce_bool(
                item.get("allow_partial_support"),
                True,
            ),
            require_counter_evidence=_coerce_bool(
                item.get("require_counter_evidence"),
                False,
            ),
            citation_granularity=(
                str(item.get("citation_granularity") or "chunk").strip() or "chunk"
            ),
        )
    raise TypeError(f"Unsupported evidence policy definition: {type(item)!r}")


def _coerce_output_contract(item: Any) -> OutputContractDefinition:
    if isinstance(item, OutputContractDefinition):
        return item
    if isinstance(item, Mapping):
        return OutputContractDefinition(
            contract_id=str(item.get("contract_id") or "").strip(),
            applies_to_intents=_coerce_string_list(item.get("applies_to_intents")),
            required_sections=_coerce_string_list(item.get("required_sections")),
            optional_sections=_coerce_string_list(item.get("optional_sections")),
            require_uncertainty_when_partial=_coerce_bool(
                item.get("require_uncertainty_when_partial"),
                True,
            ),
        )
    raise TypeError(f"Unsupported output contract definition: {type(item)!r}")


def _coerce_guardrail_policy(item: Any) -> GuardrailPolicy:
    if isinstance(item, GuardrailPolicy):
        return item
    if isinstance(item, Mapping):
        extra_flags = {
            str(key).strip(): _coerce_bool(value)
            for key, value in dict(item.get("extra_flags") or {}).items()
            if str(key).strip()
        }
        return GuardrailPolicy(
            disallow_unbacked_new_relations=_coerce_bool(
                item.get("disallow_unbacked_new_relations"),
                False,
            ),
            disallow_cross_path_fabrication=_coerce_bool(
                item.get("disallow_cross_path_fabrication"),
                False,
            ),
            require_explicit_uncertainty_on_partial_support=_coerce_bool(
                item.get("require_explicit_uncertainty_on_partial_support"),
                False,
            ),
            extra_flags=extra_flags,
            extra_constraints=_coerce_string_list(item.get("extra_constraints")),
        )
    return GuardrailPolicy()


def _coerce_node_role_rule(item: Any) -> NodeRoleRuleDefinition:
    if isinstance(item, NodeRoleRuleDefinition):
        return item
    if isinstance(item, Mapping):
        roles_by_intent = {
            str(intent).strip(): _coerce_string_list(roles)
            for intent, roles in dict(item.get("roles_by_intent") or {}).items()
            if str(intent).strip()
        }
        return NodeRoleRuleDefinition(
            node_type=str(item.get("node_type") or "").strip(),
            roles_by_intent=roles_by_intent,
        )
    raise TypeError(f"Unsupported node role rule definition: {type(item)!r}")


def _coerce_path_constraint_policy(item: Any) -> PathConstraintPolicy:
    if isinstance(item, PathConstraintPolicy):
        return item
    if isinstance(item, Mapping):
        return PathConstraintPolicy(
            default_max_hops=(
                None
                if item.get("default_max_hops") is None
                else _coerce_positive_int(item.get("default_max_hops"), 1)
            ),
            prefer_shorter_paths=(
                None
                if item.get("prefer_shorter_paths") is None
                else _coerce_bool(item.get("prefer_shorter_paths"))
            ),
            penalize_repeated_nodes=(
                None
                if item.get("penalize_repeated_nodes") is None
                else _coerce_bool(item.get("penalize_repeated_nodes"))
            ),
            require_direction_match_when_declared=(
                None
                if item.get("require_direction_match_when_declared") is None
                else _coerce_bool(item.get("require_direction_match_when_declared"))
            ),
            disallow_unknown_node_roles=(
                None
                if item.get("disallow_unknown_node_roles") is None
                else _coerce_bool(item.get("disallow_unknown_node_roles"))
            ),
        )
    return PathConstraintPolicy()


def _coerce_scenario_override(item: Any) -> ScenarioOverrideDefinition:
    if isinstance(item, ScenarioOverrideDefinition):
        return item
    if isinstance(item, Mapping):
        return ScenarioOverrideDefinition(
            scenario_id=str(item.get("scenario_id") or "").strip(),
            description=str(item.get("description") or "").strip(),
            applies_to_intent=str(item.get("applies_to_intent") or "").strip(),
            template_id=str(item.get("template_id") or "").strip() or None,
            evidence_policy_id=(
                str(item.get("evidence_policy_id") or "").strip() or None
            ),
            output_contract_id=(
                str(item.get("output_contract_id") or "").strip() or None
            ),
            guardrails=_coerce_guardrail_policy(item.get("guardrails")),
            metadata=deepcopy(dict(item.get("metadata") or {})),
        )
    raise TypeError(f"Unsupported scenario override definition: {type(item)!r}")


def _coerce_prompt_binding_policy(item: Any) -> PromptBindingPolicy:
    if isinstance(item, PromptBindingPolicy):
        return item
    if isinstance(item, Mapping):
        return PromptBindingPolicy(
            default_template_id=str(item.get("default_template_id") or "").strip(),
            template_by_intent={
                str(key).strip(): str(value).strip()
                for key, value in dict(item.get("template_by_intent") or {}).items()
                if str(key).strip() and str(value).strip()
            },
            template_by_scenario={
                str(key).strip(): str(value).strip()
                for key, value in dict(item.get("template_by_scenario") or {}).items()
                if str(key).strip() and str(value).strip()
            },
            extra_constraints=_coerce_string_list(item.get("extra_constraints")),
        )
    return PromptBindingPolicy()


def _coerce_explanation_profile(item: Any) -> ExplanationProfile | None:
    if item is None:
        return None
    if isinstance(item, ExplanationProfile):
        return item
    if isinstance(item, Mapping):
        intent_bindings = [
            binding
            for binding in (
                _coerce_intent_binding(raw) for raw in item.get("intent_bindings", [])
            )
            if binding.intent_id and binding.intent_family
        ]
        semantic_tags = [
            tag
            for tag in (
                _coerce_semantic_tag(raw) for raw in item.get("semantic_tags", [])
            )
            if tag.tag_id
        ]
        relation_semantics = [
            rule
            for rule in (
                _coerce_relation_semantic_rule(raw)
                for raw in item.get("relation_semantics", [])
            )
            if rule.relation_type
        ]
        node_role_rules = [
            rule
            for rule in (
                _coerce_node_role_rule(raw)
                for raw in item.get("node_role_rules", [])
            )
            if rule.node_type
        ]
        evidence_policies = [
            policy
            for policy in (
                _coerce_evidence_policy(raw)
                for raw in item.get("evidence_policies", [])
            )
            if policy.policy_id
        ]
        output_contracts = [
            contract
            for contract in (
                _coerce_output_contract(raw)
                for raw in item.get("output_contracts", [])
            )
            if contract.contract_id
        ]
        scenario_overrides = [
            scenario
            for scenario in (
                _coerce_scenario_override(raw)
                for raw in item.get("scenario_overrides", [])
            )
            if scenario.scenario_id
        ]
        return ExplanationProfile(
            profile_id=str(item.get("profile_id") or "").strip(),
            version=str(item.get("version") or "v1").strip() or "v1",
            extends=str(item.get("extends") or "").strip() or None,
            risk_level=str(item.get("risk_level") or "low").strip() or "low",
            supported_intents=_coerce_string_list(item.get("supported_intents")),
            intent_bindings=intent_bindings,
            semantic_tags=semantic_tags,
            relation_semantics=relation_semantics,
            node_role_rules=node_role_rules,
            path_constraints=_coerce_path_constraint_policy(
                item.get("path_constraints")
            ),
            evidence_policies=evidence_policies,
            output_contracts=output_contracts,
            guardrails=_coerce_guardrail_policy(item.get("guardrails")),
            scenario_overrides=scenario_overrides,
            prompt_bindings=_coerce_prompt_binding_policy(item.get("prompt_bindings")),
            metadata=deepcopy(dict(item.get("metadata") or {})),
        )
    raise TypeError(f"Unsupported explanation profile definition: {type(item)!r}")


def _normalize_entity_types(items: Iterable[Any]) -> list[EntityTypeDefinition]:
    normalized = []
    for item in items:
        value = _coerce_entity_type(item)
        if value.name:
            normalized.append(value)
    return normalized


def _normalize_relation_types(items: Iterable[Any]) -> list[RelationTypeDefinition]:
    normalized = []
    for item in items:
        value = _coerce_relation_type(item)
        if value.name:
            normalized.append(value)
    return normalized


def _merge_items_by_key(
    base_items: Iterable[Any],
    override_items: Iterable[Any],
    *,
    key_attr: str,
) -> list[Any]:
    merged: dict[str, Any] = {}
    ordered_keys: list[str] = []
    for item in list(base_items) + list(override_items):
        key = str(getattr(item, key_attr, "") or "").strip()
        if not key:
            continue
        if key not in merged:
            ordered_keys.append(key)
        merged[key] = item
    return [merged[key] for key in ordered_keys]


def _merge_prompt_bindings(
    base: PromptBindingPolicy,
    override: PromptBindingPolicy,
) -> PromptBindingPolicy:
    return PromptBindingPolicy(
        default_template_id=(
            override.default_template_id or base.default_template_id
        ),
        template_by_intent={
            **base.template_by_intent,
            **override.template_by_intent,
        },
        template_by_scenario={
            **base.template_by_scenario,
            **override.template_by_scenario,
        },
        extra_constraints=_merge_unique_strings(
            base.extra_constraints,
            override.extra_constraints,
        ),
    )


def _merge_guardrail_policies(
    base: GuardrailPolicy,
    override: GuardrailPolicy,
) -> GuardrailPolicy:
    merged_extra_flags = dict(base.extra_flags)
    for key, value in override.extra_flags.items():
        merged_extra_flags[key] = _coerce_bool(merged_extra_flags.get(key), False) or _coerce_bool(value, False)
    return GuardrailPolicy(
        disallow_unbacked_new_relations=(
            base.disallow_unbacked_new_relations
            or override.disallow_unbacked_new_relations
        ),
        disallow_cross_path_fabrication=(
            base.disallow_cross_path_fabrication
            or override.disallow_cross_path_fabrication
        ),
        require_explicit_uncertainty_on_partial_support=(
            base.require_explicit_uncertainty_on_partial_support
            or override.require_explicit_uncertainty_on_partial_support
        ),
        extra_flags=merged_extra_flags,
        extra_constraints=_merge_unique_strings(
            base.extra_constraints,
            override.extra_constraints,
        ),
    )


def _merge_node_role_rules(
    base_rules: Iterable[NodeRoleRuleDefinition],
    override_rules: Iterable[NodeRoleRuleDefinition],
) -> list[NodeRoleRuleDefinition]:
    merged: dict[str, NodeRoleRuleDefinition] = {}
    ordered_keys: list[str] = []
    for rule in list(base_rules) + list(override_rules):
        node_type = str(rule.node_type or "").strip()
        if not node_type:
            continue
        if node_type not in merged:
            ordered_keys.append(node_type)
            merged[node_type] = rule
            continue
        base_rule = merged[node_type]
        roles_by_intent = {
            key: list(value)
            for key, value in base_rule.roles_by_intent.items()
        }
        for intent, roles in rule.roles_by_intent.items():
            roles_by_intent[intent] = _merge_unique_strings(
                roles_by_intent.get(intent, []),
                roles,
            )
        merged[node_type] = NodeRoleRuleDefinition(
            node_type=node_type,
            roles_by_intent=roles_by_intent,
        )
    return [merged[key] for key in ordered_keys]


def _merge_path_constraint_policies(
    base: PathConstraintPolicy,
    override: PathConstraintPolicy,
) -> PathConstraintPolicy:
    return PathConstraintPolicy(
        default_max_hops=(
            override.default_max_hops
            if override.default_max_hops is not None
            else base.default_max_hops
        ),
        prefer_shorter_paths=(
            override.prefer_shorter_paths
            if override.prefer_shorter_paths is not None
            else base.prefer_shorter_paths
        ),
        penalize_repeated_nodes=(
            override.penalize_repeated_nodes
            if override.penalize_repeated_nodes is not None
            else base.penalize_repeated_nodes
        ),
        require_direction_match_when_declared=(
            override.require_direction_match_when_declared
            if override.require_direction_match_when_declared is not None
            else base.require_direction_match_when_declared
        ),
        disallow_unknown_node_roles=(
            override.disallow_unknown_node_roles
            if override.disallow_unknown_node_roles is not None
            else base.disallow_unknown_node_roles
        ),
    )


def _merge_explanation_profiles(
    base: ExplanationProfile | None,
    override: ExplanationProfile | None,
) -> ExplanationProfile | None:
    if base is None:
        return override
    if override is None:
        return base
    return ExplanationProfile(
        profile_id=override.profile_id or base.profile_id,
        version=override.version or base.version,
        extends=override.extends if override.extends is not None else base.extends,
        risk_level=override.risk_level or base.risk_level,
        supported_intents=_merge_unique_strings(
            base.supported_intents,
            override.supported_intents,
        ),
        intent_bindings=_merge_items_by_key(
            base.intent_bindings,
            override.intent_bindings,
            key_attr="intent_id",
        ),
        semantic_tags=_merge_items_by_key(
            base.semantic_tags,
            override.semantic_tags,
            key_attr="tag_id",
        ),
        relation_semantics=_merge_items_by_key(
            base.relation_semantics,
            override.relation_semantics,
            key_attr="relation_type",
        ),
        node_role_rules=_merge_node_role_rules(
            base.node_role_rules,
            override.node_role_rules,
        ),
        path_constraints=_merge_path_constraint_policies(
            base.path_constraints,
            override.path_constraints,
        ),
        evidence_policies=_merge_items_by_key(
            base.evidence_policies,
            override.evidence_policies,
            key_attr="policy_id",
        ),
        output_contracts=_merge_items_by_key(
            base.output_contracts,
            override.output_contracts,
            key_attr="contract_id",
        ),
        guardrails=_merge_guardrail_policies(
            base.guardrails,
            override.guardrails,
        ),
        scenario_overrides=_merge_items_by_key(
            base.scenario_overrides,
            override.scenario_overrides,
            key_attr="scenario_id",
        ),
        prompt_bindings=_merge_prompt_bindings(
            base.prompt_bindings,
            override.prompt_bindings,
        ),
        metadata=_deep_merge_mappings(base.metadata, override.metadata),
    )


def _merge_aliases(
    base_aliases: Mapping[str, str | list[str]],
    override_aliases: Mapping[str, str | list[str]] | None,
) -> dict[str, str | list[str]]:
    merged = dict(base_aliases)
    if override_aliases:
        merged.update(override_aliases)
    return merged


def _definition_lookup_map(
    definitions: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, str | list[str]] | None = None,
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    canonical_names: set[str] = set()

    for item in definitions:
        canonical_name = str(item.get("name") or "").strip()
        if not canonical_name:
            continue
        canonical_names.add(canonical_name)
        for candidate in (
            canonical_name,
            str(item.get("display_name") or "").strip(),
            *[
                str(alias).strip()
                for alias in item.get("aliases", [])
                if str(alias).strip()
            ],
        ):
            key = _normalize_schema_match_key(candidate)
            if key:
                lookup[key] = canonical_name

    for alias_key, alias_value in (aliases or {}).items():
        normalized_key = _normalize_schema_match_key(alias_key)
        if not normalized_key:
            continue

        if isinstance(alias_value, str):
            canonical_name = alias_value.strip()
            if canonical_name in canonical_names:
                lookup[normalized_key] = canonical_name
                continue
            if alias_key in canonical_names and canonical_name:
                lookup[_normalize_schema_match_key(canonical_name)] = alias_key
        elif isinstance(alias_value, list) and alias_key in canonical_names:
            canonical_name = alias_key
            for candidate in alias_value:
                candidate_key = _normalize_schema_match_key(candidate)
                if candidate_key:
                    lookup[candidate_key] = canonical_name

    return lookup


def _runtime_entity_type_lookup(domain_schema: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(domain_schema, Mapping):
        return {}
    return _definition_lookup_map(
        definitions=domain_schema.get("entity_types", []),
        aliases=domain_schema.get("aliases"),
    )


def _runtime_entity_type_names(domain_schema: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(domain_schema, Mapping):
        return set()

    names = {
        str(item).strip()
        for item in domain_schema.get("entity_type_names", [])
        if str(item).strip()
    }
    if names:
        return names

    return {
        str(item.get("name") or "").strip()
        for item in domain_schema.get("entity_types", [])
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }


def _schema_metadata(domain_schema: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(domain_schema, Mapping):
        return {}
    metadata = domain_schema.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _strict_entity_type_fallback(domain_schema: Mapping[str, Any] | None) -> str:
    metadata = _schema_metadata(domain_schema)
    canonicalization_mode = str(
        metadata.get("entity_type_canonicalization") or ""
    ).strip().lower()
    strict = _coerce_bool(metadata.get("strict_entity_types"), False) or (
        canonicalization_mode in {"strict", "schema_only", "schema-only"}
    )
    if not strict:
        return ""

    entity_type_names = _runtime_entity_type_names(domain_schema)
    fallback = str(
        metadata.get("fallback_entity_type")
        or metadata.get("unknown_entity_type")
        or ""
    ).strip()
    if fallback in entity_type_names:
        return fallback
    if "Other" in entity_type_names:
        return "Other"
    return ""


def _runtime_relation_type_lookup(
    domain_schema: Mapping[str, Any] | None,
) -> dict[str, str]:
    if not isinstance(domain_schema, Mapping):
        return {}
    return _definition_lookup_map(
        definitions=domain_schema.get("relation_types", []),
        aliases=None,
    )


def normalize_extracted_entity_type(
    entity_type: str,
    domain_schema: Mapping[str, Any] | None,
) -> str:
    if not isinstance(entity_type, str):
        return entity_type
    if not isinstance(domain_schema, Mapping) or not domain_schema.get("enabled"):
        return entity_type

    lookup = _runtime_entity_type_lookup(domain_schema)
    normalized = lookup.get(_normalize_schema_match_key(entity_type))
    if normalized:
        return normalized
    return _strict_entity_type_fallback(domain_schema) or entity_type


def normalize_extracted_relation_keywords(
    keywords: str,
    domain_schema: Mapping[str, Any] | None,
) -> str:
    if not isinstance(keywords, str):
        return keywords
    if not isinstance(domain_schema, Mapping) or not domain_schema.get("enabled"):
        return keywords

    lookup = _runtime_relation_type_lookup(domain_schema)
    normalized_tokens: list[str] = []
    seen: set[str] = set()
    raw_tokens = keywords.replace("，", ",").replace("；", ",").split(",")

    for token in raw_tokens:
        cleaned = token.strip()
        if not cleaned:
            continue
        canonical = lookup.get(_normalize_schema_match_key(cleaned), cleaned)
        if canonical in seen:
            continue
        normalized_tokens.append(canonical)
        seen.add(canonical)

    return ",".join(normalized_tokens)


def get_builtin_explanation_profile_registry() -> dict[str, ExplanationProfile]:
    registry: dict[str, ExplanationProfile] = {}
    for schema in get_builtin_schema_registry().values():
        profile = schema.explanation_profile
        if profile is not None and profile.profile_id:
            registry[profile.profile_id] = profile
    return registry


def _resolve_explanation_profile_inheritance(
    profile: ExplanationProfile | None,
    registry: Mapping[str, ExplanationProfile],
    *,
    seen: set[str] | None = None,
) -> ExplanationProfile | None:
    if profile is None:
        return None
    if not profile.extends:
        return profile

    profile_id = profile.profile_id or profile.extends
    lineage = set(seen or set())
    if profile_id in lineage:
        raise ValueError(
            f"Cyclic explanation profile inheritance detected at '{profile_id}'"
        )
    lineage.add(profile_id)

    parent = registry.get(profile.extends)
    if parent is None:
        raise ValueError(
            f"Unknown explanation profile parent '{profile.extends}' for '{profile_id}'"
        )

    resolved_parent = _resolve_explanation_profile_inheritance(
        parent,
        registry,
        seen=lineage,
    )
    return _merge_explanation_profiles(resolved_parent, profile)


def get_builtin_schema_registry() -> dict[str, DomainSchema]:
    from lightrag_fork.schemas.economy import ECONOMY_DOMAIN_SCHEMA
    from lightrag_fork.schemas.general import GENERAL_DOMAIN_SCHEMA

    return {
        GENERAL_DOMAIN_SCHEMA.profile_name: GENERAL_DOMAIN_SCHEMA,
        ECONOMY_DOMAIN_SCHEMA.profile_name: ECONOMY_DOMAIN_SCHEMA,
    }


def resolve_domain_schema(raw_config: Mapping[str, Any] | None = None) -> DomainSchema:
    config = dict(raw_config or {})
    registry = get_builtin_schema_registry()
    explanation_registry = get_builtin_explanation_profile_registry()

    profile_name = str(
        config.get("profile_name") or DEFAULT_DOMAIN_SCHEMA_PROFILE
    ).strip() or DEFAULT_DOMAIN_SCHEMA_PROFILE
    if profile_name not in registry:
        valid_names = ", ".join(sorted(registry))
        raise ValueError(
            f"Unknown domain schema profile '{profile_name}'. Valid profiles: {valid_names}"
        )

    base_schema = registry[profile_name]
    resolved_explanation_profile = _resolve_explanation_profile_inheritance(
        base_schema.explanation_profile,
        explanation_registry,
    )
    if "explanation_profile" in config:
        override_profile = _coerce_explanation_profile(config.get("explanation_profile"))
        if override_profile is not None:
            if not override_profile.profile_id and resolved_explanation_profile is not None:
                override_profile = replace(
                    override_profile,
                    profile_id=resolved_explanation_profile.profile_id,
                )
            resolved_explanation_profile = _merge_explanation_profiles(
                resolved_explanation_profile,
                override_profile,
            )

    resolved = replace(
        base_schema,
        enabled=bool(config.get("enabled", base_schema.enabled)),
        mode=str(config.get("mode", base_schema.mode) or base_schema.mode),
        profile_name=profile_name,
        domain_name=str(config.get("domain_name", base_schema.domain_name) or base_schema.domain_name),
        language=str(config.get("language", base_schema.language) or base_schema.language),
        description=str(config.get("description", base_schema.description) or base_schema.description),
        explanation_profile=resolved_explanation_profile,
        aliases=_merge_aliases(base_schema.aliases, config.get("aliases")),
        extraction_rules=[
            str(rule).strip()
            for rule in config.get("extraction_rules", base_schema.extraction_rules)
            if str(rule).strip()
        ],
        metadata=deepcopy(config.get("metadata", base_schema.metadata)),
    )

    if "entity_types" in config:
        resolved = replace(
            resolved,
            entity_types=_normalize_entity_types(config.get("entity_types", [])),
        )

    if "relation_types" in config:
        resolved = replace(
            resolved,
            relation_types=_normalize_relation_types(config.get("relation_types", [])),
        )

    return resolved


def normalize_addon_schema_config(addon_params: Mapping[str, Any] | None) -> dict[str, Any]:
    addon = dict(addon_params or {})
    raw_schema = addon.get("domain_schema")
    if not isinstance(raw_schema, Mapping):
        raw_schema = {}

    resolved_schema = resolve_domain_schema(raw_schema).to_runtime_dict()
    addon["domain_schema"] = resolved_schema

    # Preserve current generic behavior unless schema mode is explicitly enabled.
    if resolved_schema["enabled"]:
        profile_name = str(raw_schema.get("profile_name") or resolved_schema["profile_name"])

        addon["language"] = str(
            raw_schema.get("language")
            or addon.get("language")
            or resolved_schema["language"]
            or DEFAULT_SUMMARY_LANGUAGE
        )

        if "entity_types" in raw_schema:
            addon["entity_types"] = list(resolved_schema["entity_type_names"])
        elif profile_name != DEFAULT_DOMAIN_SCHEMA_PROFILE:
            addon["entity_types"] = list(resolved_schema["entity_type_names"])
        elif not addon.get("entity_types"):
            addon["entity_types"] = list(resolved_schema["entity_type_names"])

    addon.setdefault("language", DEFAULT_SUMMARY_LANGUAGE)
    addon.setdefault("entity_types", list(DEFAULT_ENTITY_TYPES))
    return addon
