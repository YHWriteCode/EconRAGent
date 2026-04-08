from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from kg_agent.agent.prompts import build_path_explainer_prompt


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
RELATION_QUESTION_PATTERN = re.compile(
    r"(为什么|影响|传导|how|why|impact|affect|cause|driv)",
    re.IGNORECASE,
)
SEMANTIC_TAG_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "__cause__",
        ("why", "cause", "reason", "导致", "原因", "为何", "为什么", "传导"),
    ),
    (
        "__impact__",
        ("impact", "affect", "effect", "drive", "推动", "带动", "影响", "拉动"),
    ),
    (
        "__increase__",
        ("increase", "growth", "rise", "surge", "增长", "提升", "扩张", "改善"),
    ),
    (
        "__decrease__",
        ("decrease", "decline", "drop", "fall", "压缩", "下降", "减少", "下滑"),
    ),
    (
        "__policy__",
        ("policy", "regulation", "subsid", "政策", "监管", "补贴"),
    ),
    (
        "__cost__",
        ("cost", "price", "margin", "expense", "成本", "价格", "利润", "毛利"),
    ),
    ("__risk__", ("risk", "pressure", "uncertain", "风险", "压力", "波动")),
)
RELATION_EXPLANATION_FAMILIES = {
    "causal_explanation",
    "diagnostic_reasoning",
    "differential_reasoning",
}
INTENT_ROLE_EXPECTATIONS: dict[str, dict[str, set[str]]] = {
    "causal_explanation": {
        "start": {"driver", "constraint", "subject_entity"},
        "middle": {
            "intermediate_entity",
            "industry_context",
            "country_context",
            "subject_entity",
            "affected_entity",
        },
        "end": {
            "affected_entity",
            "subject_entity",
            "outcome",
            "measured_target",
            "attribute_target",
        },
    },
    "containment_trace": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"grouping_entity", "intermediate_entity"},
        "end": {"container_entity", "grouping_entity", "subject_entity"},
    },
    "attribute_trace": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"intermediate_entity", "subject_entity"},
        "end": {"attribute_value", "attribute_target", "country_context"},
    },
    "responsibility_trace": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"intermediate_entity", "responsible_party"},
        "end": {"responsible_party", "owner_entity"},
    },
    "provenance_trace": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"intermediate_entity", "source_document", "document_entity"},
        "end": {"source_document", "document_entity", "version_entity"},
    },
    "prerequisite_trace": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"intermediate_entity"},
        "end": {"subject_entity", "attribute_target"},
    },
    "process_trace": {
        "start": {"driver", "subject_entity"},
        "middle": {"intermediate_entity", "subject_entity"},
        "end": {"outcome", "attribute_target", "subject_entity"},
    },
    "comparison_explanation": {
        "start": {"subject_entity", "member_entity"},
        "middle": {"intermediate_entity", "attribute_target"},
        "end": {"subject_entity", "attribute_target", "outcome"},
    },
}
EVIDENCE_SCORE_THRESHOLD = 3.0
PATH_SCORE_THRESHOLD = 4.0


def _normalize_ascii_token(token: str) -> str:
    lowered = token.lower()
    if lowered.endswith("ies") and len(lowered) > 4:
        return lowered[:-3] + "y"
    for suffix in ("ing", "ed", "ly", "es", "s"):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            return lowered[: -len(suffix)]
    return lowered


def _expand_cjk_token(token: str) -> set[str]:
    cleaned = token.strip()
    if not cleaned:
        return set()
    tokens = {cleaned}
    if len(cleaned) <= 3:
        tokens.update(cleaned)
    span = cleaned[:48]
    for size in (2, 3, 4):
        if len(span) < size:
            break
        for index in range(len(span) - size + 1):
            tokens.add(span[index : index + size])
    return tokens


def _normalize_match_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _normalize_relation_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        char for char in value.strip().lower() if char.isalnum() or char == "_"
    )


def _normalize_type_key(value: Any) -> str:
    return _normalize_relation_key(value)


def _semantic_tag_token(tag_id: Any) -> str:
    normalized = str(tag_id or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("__") and normalized.endswith("__"):
        return normalized
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", normalized).strip("_").lower()
    if not normalized:
        return ""
    return f"__{normalized}__"


def _resolve_explanation_profile(
    domain_schema: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(domain_schema, Mapping):
        return None
    profile = domain_schema.get("explanation_profile")
    return profile if isinstance(profile, dict) else None


def _legacy_extract_semantic_tags(text: str) -> set[str]:
    lowered = (text or "").lower()
    tags = set()
    for tag, patterns in SEMANTIC_TAG_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            tags.add(tag)
    return tags


def _extract_semantic_tags(
    text: str,
    explanation_profile: Mapping[str, Any] | None = None,
) -> set[str]:
    if explanation_profile is None:
        return _legacy_extract_semantic_tags(text)

    lowered = (text or "").lower()
    tags: set[str] = set()
    for raw_tag in explanation_profile.get("semantic_tags", []):
        if not isinstance(raw_tag, Mapping):
            continue
        tag_token = _semantic_tag_token(
            raw_tag.get("tag_id") or raw_tag.get("name") or ""
        )
        if not tag_token:
            continue
        aliases = [
            raw_tag.get("tag_id"),
            *list(raw_tag.get("aliases") or []),
        ]
        if any(
            normalized and normalized in lowered
            for normalized in (_normalize_match_text(value) for value in aliases)
        ):
            tags.add(tag_token)
    return tags


def _count_trigger_hits(lowered_query: str, triggers: list[Any]) -> int:
    hits = 0
    for trigger in triggers:
        normalized = _normalize_match_text(trigger)
        if normalized and normalized in lowered_query:
            hits += 1
    return hits


def _resolve_query_intent(
    query: str,
    explanation_profile: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if explanation_profile is None:
        return None

    lowered_query = _normalize_match_text(query)
    query_tags = _extract_semantic_tags(query, explanation_profile)
    best_score = 0.0
    best_binding: dict[str, Any] | None = None

    for raw_binding in explanation_profile.get("intent_bindings", []):
        if not isinstance(raw_binding, Mapping):
            continue
        binding = dict(raw_binding)
        matched_triggers = [
            _normalize_match_text(trigger)
            for trigger in list(binding.get("triggers") or [])
            if _normalize_match_text(trigger)
            and _normalize_match_text(trigger) in lowered_query
        ]
        trigger_hits = len(matched_triggers)
        if trigger_hits <= 0:
            continue
        negative_hits = _count_trigger_hits(
            lowered_query,
            list(binding.get("negative_triggers") or []),
        )
        if negative_hits > 0:
            continue

        score = float(trigger_hits) + (
            max(len(trigger) for trigger in matched_triggers) / 100.0
            if matched_triggers
            else 0.0
        )
        for preferred_tag in list(binding.get("preferred_semantic_tags") or []):
            if _semantic_tag_token(preferred_tag) in query_tags:
                score += 0.1

        if score > best_score:
            best_score = score
            best_binding = binding

    return best_binding


def _legacy_question_type(query: str) -> str:
    return "relation_explanation" if RELATION_QUESTION_PATTERN.search(query or "") else "path_trace"


def _map_intent_family_to_question_type(intent_family: str | None) -> str:
    if intent_family in RELATION_EXPLANATION_FAMILIES:
        return "relation_explanation"
    return "path_trace"


def _resolve_template_id(
    explanation_profile: Mapping[str, Any] | None,
    intent_binding: Mapping[str, Any] | None,
    scenario_override: Mapping[str, Any] | None = None,
) -> str | None:
    scenario_id = ""
    if isinstance(scenario_override, Mapping):
        explicit_template = str(scenario_override.get("template_id") or "").strip()
        if explicit_template:
            return explicit_template
        scenario_id = str(scenario_override.get("scenario_id") or "").strip()

    if isinstance(intent_binding, Mapping):
        explicit_template = str(intent_binding.get("template_id") or "").strip()
        if explicit_template:
            return explicit_template
        intent_family = str(intent_binding.get("intent_family") or "").strip()
    else:
        intent_family = ""

    if not isinstance(explanation_profile, Mapping):
        return None

    prompt_bindings = explanation_profile.get("prompt_bindings")
    if not isinstance(prompt_bindings, Mapping):
        return None

    template_by_scenario = prompt_bindings.get("template_by_scenario")
    if isinstance(template_by_scenario, Mapping) and scenario_id:
        template_id = str(template_by_scenario.get(scenario_id) or "").strip()
        if template_id:
            return template_id

    template_by_intent = prompt_bindings.get("template_by_intent")
    if isinstance(template_by_intent, Mapping):
        template_id = str(template_by_intent.get(intent_family) or "").strip()
        if template_id:
            return template_id

    default_template_id = str(prompt_bindings.get("default_template_id") or "").strip()
    return default_template_id or None


def _resolve_scenario_override(
    explanation_profile: Mapping[str, Any] | None,
    intent_binding: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(explanation_profile, Mapping) or not isinstance(intent_binding, Mapping):
        return None

    scenario_id = str(intent_binding.get("scenario_id") or "").strip()
    if not scenario_id:
        return None

    intent_family = str(intent_binding.get("intent_family") or "").strip()
    for raw_override in explanation_profile.get("scenario_overrides", []):
        if not isinstance(raw_override, Mapping):
            continue
        candidate_id = str(raw_override.get("scenario_id") or "").strip()
        if candidate_id != scenario_id:
            continue
        applies_to_intent = str(raw_override.get("applies_to_intent") or "").strip()
        if applies_to_intent and intent_family and applies_to_intent != intent_family:
            continue
        return dict(raw_override)

    return None


def _resolve_evidence_policy(
    explanation_profile: Mapping[str, Any] | None,
    intent_binding: Mapping[str, Any] | None,
    scenario_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(explanation_profile, Mapping):
        return None

    intent_family = ""
    policy_id = ""
    if isinstance(scenario_override, Mapping):
        policy_id = str(scenario_override.get("evidence_policy_id") or "").strip()
    if isinstance(intent_binding, Mapping):
        intent_family = str(intent_binding.get("intent_family") or "").strip()
        if not policy_id:
            policy_id = str(intent_binding.get("evidence_policy_id") or "").strip()

    for raw_policy in explanation_profile.get("evidence_policies", []):
        if not isinstance(raw_policy, Mapping):
            continue
        if policy_id and str(raw_policy.get("policy_id") or "").strip() == policy_id:
            return dict(raw_policy)

    if intent_family:
        for raw_policy in explanation_profile.get("evidence_policies", []):
            if not isinstance(raw_policy, Mapping):
                continue
            applies_to = {
                str(item).strip()
                for item in list(raw_policy.get("applies_to_intents") or [])
                if str(item).strip()
            }
            if intent_family in applies_to:
                return dict(raw_policy)

    return None


def _resolve_output_contract(
    explanation_profile: Mapping[str, Any] | None,
    intent_binding: Mapping[str, Any] | None,
    scenario_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(explanation_profile, Mapping):
        return None

    intent_family = ""
    contract_id = ""
    if isinstance(scenario_override, Mapping):
        contract_id = str(scenario_override.get("output_contract_id") or "").strip()
    if isinstance(intent_binding, Mapping):
        intent_family = str(intent_binding.get("intent_family") or "").strip()
        if not contract_id:
            contract_id = str(intent_binding.get("output_contract_id") or "").strip()

    for raw_contract in explanation_profile.get("output_contracts", []):
        if not isinstance(raw_contract, Mapping):
            continue
        if contract_id and str(raw_contract.get("contract_id") or "").strip() == contract_id:
            return dict(raw_contract)

    if intent_family:
        for raw_contract in explanation_profile.get("output_contracts", []):
            if not isinstance(raw_contract, Mapping):
                continue
            applies_to = {
                str(item).strip()
                for item in list(raw_contract.get("applies_to_intents") or [])
                if str(item).strip()
            }
            if intent_family in applies_to:
                return dict(raw_contract)

    return None


def _resolve_guardrails(
    explanation_profile: Mapping[str, Any] | None,
    scenario_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    profile_guardrails = (
        dict(explanation_profile.get("guardrails"))
        if isinstance(explanation_profile, Mapping)
        and isinstance(explanation_profile.get("guardrails"), Mapping)
        else {}
    )
    scenario_guardrails = (
        dict(scenario_override.get("guardrails"))
        if isinstance(scenario_override, Mapping)
        and isinstance(scenario_override.get("guardrails"), Mapping)
        else {}
    )
    if not profile_guardrails and not scenario_guardrails:
        return None
    return _merge_runtime_guardrails(profile_guardrails, scenario_guardrails)


def _resolve_path_constraints(
    explanation_profile: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(explanation_profile, Mapping):
        return None
    path_constraints = explanation_profile.get("path_constraints")
    if not isinstance(path_constraints, Mapping):
        return None
    return dict(path_constraints)


def _extract_node_identifier(node: Mapping[str, Any]) -> str:
    for key in ("id", "name", "entity", "entity_name", "label"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_node_type(node: Mapping[str, Any]) -> str:
    for key in ("entity_type", "node_type", "type", "category", "kind"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_node_role_map(
    explanation_profile: Mapping[str, Any] | None,
) -> dict[str, dict[str, set[str]]]:
    if not isinstance(explanation_profile, Mapping):
        return {}

    role_map: dict[str, dict[str, set[str]]] = {}
    for raw_rule in explanation_profile.get("node_role_rules", []):
        if not isinstance(raw_rule, Mapping):
            continue
        node_type = _normalize_type_key(raw_rule.get("node_type"))
        if not node_type:
            continue
        roles_by_intent = raw_rule.get("roles_by_intent")
        if not isinstance(roles_by_intent, Mapping):
            continue
        intent_map = role_map.setdefault(node_type, {})
        for intent_name, roles in roles_by_intent.items():
            normalized_intent = str(intent_name).strip()
            if not normalized_intent:
                continue
            normalized_roles = {
                str(role).strip()
                for role in list(roles or [])
                if str(role).strip()
            }
            if not normalized_roles:
                continue
            intent_map.setdefault(normalized_intent, set()).update(normalized_roles)
    return role_map


def _constraint_bool(
    path_constraints: Mapping[str, Any] | None,
    key: str,
    default: bool,
) -> bool:
    if not isinstance(path_constraints, Mapping):
        return default
    value = path_constraints.get(key)
    if isinstance(value, bool):
        return value
    return default


def _constraint_positive_int(
    path_constraints: Mapping[str, Any] | None,
    key: str,
) -> int | None:
    if not isinstance(path_constraints, Mapping):
        return None
    value = path_constraints.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def _merge_runtime_guardrails(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "extra_flags":
            merged_flags = dict(merged.get("extra_flags") or {})
            for flag_key, flag_value in dict(value or {}).items():
                merged_flags[str(flag_key).strip()] = bool(
                    merged_flags.get(flag_key, False)
                ) or bool(flag_value)
            merged["extra_flags"] = merged_flags
        elif key == "extra_constraints":
            merged_constraints = [
                str(item).strip()
                for item in list(merged.get("extra_constraints") or [])
                if str(item).strip()
            ]
            for item in list(value or []):
                normalized = str(item).strip()
                if normalized and normalized not in merged_constraints:
                    merged_constraints.append(normalized)
            merged["extra_constraints"] = merged_constraints
        elif isinstance(value, bool):
            merged[key] = bool(merged.get(key, False)) or value
        else:
            merged[key] = value
    return merged


def _policy_requires_evidence(evidence_policy: Mapping[str, Any] | None) -> bool:
    if not isinstance(evidence_policy, Mapping):
        return True
    return bool(evidence_policy.get("require_evidence", True))


def _policy_requires_per_hop_support(
    evidence_policy: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(evidence_policy, Mapping):
        return False
    return bool(evidence_policy.get("require_per_hop_support", False))


def _policy_allows_partial_support(
    evidence_policy: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(evidence_policy, Mapping):
        return False
    return bool(evidence_policy.get("allow_partial_support", False))


def _policy_min_supporting_chunks(
    evidence_policy: Mapping[str, Any] | None,
) -> int:
    if not isinstance(evidence_policy, Mapping):
        return 1
    try:
        return max(1, int(evidence_policy.get("min_supporting_chunks", 1)))
    except (TypeError, ValueError):
        return 1


def _contract_requires_uncertainty(
    output_contract: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(output_contract, Mapping):
        return False
    return bool(output_contract.get("require_uncertainty_when_partial", False))


def _guardrail_flag(
    guardrails: Mapping[str, Any] | None,
    flag_name: str,
) -> bool:
    if not isinstance(guardrails, Mapping):
        return False
    direct_value = guardrails.get(flag_name)
    if isinstance(direct_value, bool):
        return direct_value
    extra_flags = guardrails.get("extra_flags")
    if isinstance(extra_flags, Mapping):
        extra_value = extra_flags.get(flag_name)
        if isinstance(extra_value, bool):
            return extra_value
    return False


def _match_relation_semantic_rules(
    edge: Mapping[str, Any],
    explanation_profile: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(explanation_profile, Mapping):
        return []
    relation_candidates = {
        _normalize_relation_key(edge.get(key))
        for key in ("relation", "type", "label", "description")
    }
    relation_candidates.discard("")
    if not relation_candidates:
        return []

    matched_rules: list[dict[str, Any]] = []
    for raw_rule in explanation_profile.get("relation_semantics", []):
        if not isinstance(raw_rule, Mapping):
            continue
        candidate_keys = {
            _normalize_relation_key(raw_rule.get("relation_type")),
            *[
                _normalize_relation_key(alias)
                for alias in list(raw_rule.get("aliases") or [])
            ],
        }
        candidate_keys.discard("")
        if relation_candidates & candidate_keys:
            matched_rules.append(dict(raw_rule))
    return matched_rules


def _tokenize(
    text: str,
    explanation_profile: Mapping[str, Any] | None = None,
) -> set[str]:
    tokens: set[str] = set()
    for match in TOKEN_PATTERN.finditer(text or ""):
        token = match.group(0).strip()
        if not token:
            continue
        if token.isascii():
            tokens.add(token.lower())
            tokens.add(_normalize_ascii_token(token))
        else:
            tokens.update(_expand_cjk_token(token))
    tokens.update(_extract_semantic_tags(text, explanation_profile))
    return {token for token in tokens if token}


def _normalized_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1.0, (len(left) * len(right)) ** 0.5)


@dataclass
class ExplainedPath:
    path_text: str
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class PathExplanation:
    enabled: bool
    question_type: str
    core_entities: list[str]
    paths: list[ExplainedPath]
    final_explanation: str
    uncertainty: str | None = None


@dataclass
class EvidenceSelection:
    evidence: list[str] = field(default_factory=list)
    supporting_chunk_count: int = 0
    fully_supported: bool = False
    matched_hops: int = 0
    total_hops: int = 0
    policy_id: str | None = None


class PathExplainer:
    def __init__(self, *, llm_client=None):
        self.llm_client = llm_client

    @staticmethod
    def _is_relation_explanation_query(
        query: str,
        explanation_profile: Mapping[str, Any] | None = None,
    ) -> bool:
        intent_binding = _resolve_query_intent(query, explanation_profile)
        if intent_binding is not None:
            return (
                _map_intent_family_to_question_type(
                    str(intent_binding.get("intent_family") or "").strip()
                )
                == "relation_explanation"
            )
        return bool(RELATION_QUESTION_PATTERN.search(query or ""))

    async def explain(
        self,
        query: str,
        graph_paths: list[dict[str, Any]],
        evidence_chunks: list[str],
        domain_schema: dict[str, Any] | None = None,
    ) -> PathExplanation:
        explanation_profile = _resolve_explanation_profile(domain_schema)
        intent_binding = _resolve_query_intent(query, explanation_profile)
        intent_family = (
            str(intent_binding.get("intent_family") or "").strip()
            if intent_binding is not None
            else None
        )
        scenario_override = _resolve_scenario_override(explanation_profile, intent_binding)
        scenario_id = (
            str(scenario_override.get("scenario_id") or "").strip()
            if isinstance(scenario_override, Mapping)
            else (
                str(intent_binding.get("scenario_id") or "").strip()
                if isinstance(intent_binding, Mapping)
                else ""
            )
        ) or None
        template_id = _resolve_template_id(
            explanation_profile,
            intent_binding,
            scenario_override,
        )
        evidence_policy = _resolve_evidence_policy(
            explanation_profile,
            intent_binding,
            scenario_override,
        )
        output_contract = _resolve_output_contract(
            explanation_profile,
            intent_binding,
            scenario_override,
        )
        guardrails = _resolve_guardrails(explanation_profile, scenario_override)

        normalized_paths = [
            path
            for path in graph_paths[:3]
            if isinstance(path, dict) and path.get("path_text")
        ]
        if not normalized_paths or (not evidence_chunks and _policy_requires_evidence(evidence_policy)):
            return self._build_fallback_result(
                query=query,
                intent_family=intent_family,
            )

        scored_paths = [
            (
                self._score_path(
                    query,
                    path,
                    evidence_chunks,
                    explanation_profile=explanation_profile,
                    intent_binding=intent_binding,
                    intent_family=intent_family,
                    evidence_policy=evidence_policy,
                ),
                path,
            )
            for path in normalized_paths
        ]
        scored_paths.sort(key=lambda item: item[0], reverse=True)
        best_score, best_path = scored_paths[0]
        if best_score < PATH_SCORE_THRESHOLD:
            return self._build_fallback_result(
                query=query,
                intent_family=intent_family,
            )

        evidence_selection = self._select_evidence(
            best_path,
            evidence_chunks,
            explanation_profile=explanation_profile,
            evidence_policy=evidence_policy,
        )
        if not evidence_selection.evidence and _policy_requires_evidence(evidence_policy):
            return self._build_fallback_result(
                query=query,
                intent_family=intent_family,
            )
        if (
            evidence_selection.evidence
            and not evidence_selection.fully_supported
            and not _policy_allows_partial_support(evidence_policy)
        ):
            return self._build_fallback_result(
                query=query,
                intent_family=intent_family,
            )

        explained_path = ExplainedPath(
            path_text=best_path["path_text"],
            nodes=list(best_path.get("nodes", [])),
            edges=list(best_path.get("edges", [])),
            evidence=evidence_selection.evidence,
            confidence=min(
                0.95,
                min(0.64, 0.35 + min(best_score / 25.0, 0.5))
                if not evidence_selection.fully_supported
                else 0.35 + min(best_score / 25.0, 0.5),
            ),
        )

        final_explanation = await self._build_final_explanation(
            query=query,
            explained_path=explained_path,
            domain_schema=domain_schema,
            explanation_profile=explanation_profile,
            intent_family=intent_family,
            template_id=template_id,
            scenario_id=scenario_id,
            scenario_override=scenario_override,
            evidence_policy=evidence_policy,
            output_contract=output_contract,
            guardrails=guardrails,
            support_is_partial=not evidence_selection.fully_supported,
        )
        question_type = (
            _map_intent_family_to_question_type(intent_family)
            if intent_family
            else _legacy_question_type(query)
        )

        core_entities = [
            node.get("id")
            for node in explained_path.nodes
            if isinstance(node, dict) and node.get("id")
        ][:3]
        uncertainty = self._build_uncertainty(
            query=query,
            explained_path=explained_path,
            evidence_selection=evidence_selection,
            explanation_profile=explanation_profile,
            intent_binding=intent_binding,
            evidence_policy=evidence_policy,
            output_contract=output_contract,
            guardrails=guardrails,
        )
        return PathExplanation(
            enabled=True,
            question_type=question_type,
            core_entities=core_entities,
            paths=[explained_path],
            final_explanation=final_explanation,
            uncertainty=uncertainty,
        )

    def _score_path(
        self,
        query: str,
        path: dict[str, Any],
        evidence_chunks: list[str],
        explanation_profile: Mapping[str, Any] | None = None,
        intent_binding: Mapping[str, Any] | None = None,
        intent_family: str | None = None,
        evidence_policy: Mapping[str, Any] | None = None,
    ) -> float:
        query_tokens = _tokenize(query, explanation_profile)
        path_signals = self._build_path_signals(path, explanation_profile)
        path_constraints = _resolve_path_constraints(explanation_profile)
        evidence_scores = [
            self._score_evidence_chunk(
                query_tokens=query_tokens,
                path_signals=path_signals,
                chunk=chunk,
                explanation_profile=explanation_profile,
            )
            for chunk in evidence_chunks
        ]
        best_evidence_score = max(evidence_scores, default=0.0)
        supporting_chunk_count = sum(
            1 for score in evidence_scores if score >= EVIDENCE_SCORE_THRESHOLD
        )
        top_support_scores = sorted(evidence_scores, reverse=True)[:2]
        average_top_support = (
            sum(top_support_scores) / len(top_support_scores)
            if top_support_scores
            else 0.0
        )
        exact_query_mentions = sum(
            1
            for phrase in path_signals["node_phrases"]
            if phrase and phrase in query
        )
        hop_count = max(
            len(path.get("edges", [])),
            max(0, len(path.get("nodes", [])) - 1),
        )
        structural_bonus, structural_penalty = self._score_structural_fit(
            path_signals=path_signals,
            intent_family=intent_family,
            explanation_profile=explanation_profile,
            path_constraints=path_constraints,
            hop_count=hop_count,
        )
        relation_bonus = self._score_relation_semantics_fit(
            path_signals=path_signals,
            intent_binding=intent_binding,
            intent_family=intent_family,
            path_constraints=path_constraints,
        )
        policy_bonus = 0.0
        if _policy_requires_per_hop_support(evidence_policy):
            supported_hops = self._count_supported_hops(
                path=path,
                chunks=evidence_chunks,
                path_signals=path_signals,
                explanation_profile=explanation_profile,
            )
            total_edges = len(
                [edge for edge in path.get("edges", []) if isinstance(edge, dict)]
            )
            policy_bonus += min(2.5, supported_hops * 0.9)
            if total_edges and supported_hops < total_edges:
                policy_bonus -= (total_edges - supported_hops) * 0.8
        return (
            _normalized_overlap(query_tokens, path_signals["path_tokens"]) * 8.0
            + _normalized_overlap(query_tokens, path_signals["node_tokens"]) * 7.0
            + _normalized_overlap(query_tokens, path_signals["edge_tokens"]) * 4.0
            + exact_query_mentions * 2.0
            + best_evidence_score * 1.4
            + average_top_support * 0.45
            + supporting_chunk_count * 0.6
            + structural_bonus
            + relation_bonus
            + policy_bonus
            - structural_penalty
        )

    @staticmethod
    def _score_relation_semantics_fit(
        *,
        path_signals: Mapping[str, Any],
        intent_binding: Mapping[str, Any] | None,
        intent_family: str | None,
        path_constraints: Mapping[str, Any] | None = None,
    ) -> float:
        bonus = 0.0
        if isinstance(intent_binding, Mapping):
            preferred_relation_types = {
                str(value).strip()
                for value in list(intent_binding.get("preferred_relation_types") or [])
                if str(value).strip()
            }
            preferred_semantic_tags = {
                _semantic_tag_token(value)
                for value in list(intent_binding.get("preferred_semantic_tags") or [])
            }
            preferred_semantic_tags.discard("")

            matched_relation_types = {
                str(value).strip()
                for value in set(path_signals.get("relation_types") or set())
                if str(value).strip()
            }
            matched_relation_tags = {
                str(value).strip()
                for value in set(path_signals.get("relation_tags") or set())
                if str(value).strip()
            }

            if preferred_relation_types and matched_relation_types:
                overlap = preferred_relation_types & matched_relation_types
                bonus += len(overlap) * 2.5
                if not overlap:
                    bonus -= 0.8
            if preferred_semantic_tags and matched_relation_tags:
                bonus += len(preferred_semantic_tags & matched_relation_tags) * 0.9

        if intent_family:
            for rule in list(path_signals.get("matched_relation_rules") or []):
                if not isinstance(rule, Mapping):
                    continue
                allowed_intents = {
                    str(value).strip()
                    for value in list(rule.get("allowed_intents") or [])
                    if str(value).strip()
                }
                if not allowed_intents:
                    continue
                bonus += 0.6 if intent_family in allowed_intents else -0.5

        if _constraint_bool(
            path_constraints,
            "require_direction_match_when_declared",
            False,
        ):
            bonus += float(path_signals.get("direction_match_count", 0)) * 0.45
            bonus -= float(path_signals.get("direction_mismatch_count", 0)) * 0.9

        return bonus

    @staticmethod
    def _score_structural_fit(
        *,
        path_signals: Mapping[str, Any],
        intent_family: str | None,
        explanation_profile: Mapping[str, Any] | None,
        path_constraints: Mapping[str, Any] | None,
        hop_count: int,
    ) -> tuple[float, float]:
        if path_constraints is None:
            compact_bonus = 1.0 if 1 <= hop_count <= 3 else 0.0
            hop_penalty = max(0, hop_count - 3) * 1.1
            return compact_bonus, hop_penalty

        max_hops = _constraint_positive_int(path_constraints, "default_max_hops")
        prefer_shorter_paths = _constraint_bool(
            path_constraints,
            "prefer_shorter_paths",
            True,
        )
        penalize_repeated_nodes = _constraint_bool(
            path_constraints,
            "penalize_repeated_nodes",
            False,
        )
        disallow_unknown_node_roles = _constraint_bool(
            path_constraints,
            "disallow_unknown_node_roles",
            False,
        )

        compact_limit = min(max_hops, 3) if max_hops is not None else 3
        compact_bonus = 1.0 if 1 <= hop_count <= compact_limit else 0.0
        hop_penalty = 0.0
        if prefer_shorter_paths:
            hop_penalty += max(0, hop_count - compact_limit) * 1.1
        if max_hops is not None and hop_count > max_hops:
            hop_penalty += (hop_count - max_hops) * 1.4

        if penalize_repeated_nodes:
            hop_penalty += float(path_signals.get("repeated_node_count", 0)) * 1.2

        role_bonus = 0.0
        role_penalty = 0.0
        if intent_family:
            role_map = _resolve_node_role_map(explanation_profile)
            node_types = [
                str(node_type).strip()
                for node_type in list(path_signals.get("node_types") or [])
                if str(node_type).strip()
            ]
            total_nodes = len(node_types)
            for index, node_type in enumerate(node_types):
                normalized_type = _normalize_type_key(node_type)
                intent_roles = role_map.get(normalized_type, {}).get(intent_family, set())
                position = (
                    "start"
                    if index == 0
                    else "end"
                    if index == total_nodes - 1
                    else "middle"
                )
                expected_roles = INTENT_ROLE_EXPECTATIONS.get(intent_family, {}).get(
                    position,
                    set(),
                )
                weight = 1.0 if position in {"start", "end"} else 0.65
                if intent_roles and expected_roles:
                    if intent_roles & expected_roles:
                        role_bonus += 0.85 * weight
                    else:
                        role_penalty += 0.7 * weight
                elif normalized_type and disallow_unknown_node_roles:
                    role_penalty += 0.55 * weight

        return compact_bonus + role_bonus, hop_penalty + role_penalty

    def _count_supported_hops(
        self,
        *,
        path: dict[str, Any],
        chunks: list[str],
        path_signals: Mapping[str, Any],
        explanation_profile: Mapping[str, Any] | None,
    ) -> int:
        edges = [edge for edge in path.get("edges", []) if isinstance(edge, dict)]
        edge_support_terms = list(path_signals.get("edge_support_terms") or [])
        matched_hops = 0
        for index, edge in enumerate(edges):
            support_terms = edge_support_terms[index] if index < len(edge_support_terms) else set()
            if any(
                self._chunk_supports_edge(
                    chunk=chunk,
                    edge=edge,
                    support_terms=support_terms,
                    explanation_profile=explanation_profile,
                )
                for chunk in chunks
            ):
                matched_hops += 1
        return matched_hops

    @staticmethod
    def _chunk_supports_edge(
        *,
        chunk: str,
        edge: Mapping[str, Any],
        support_terms: set[str],
        explanation_profile: Mapping[str, Any] | None,
    ) -> bool:
        if not chunk:
            return False
        source = edge.get("source")
        target = edge.get("target")
        source_in_chunk = isinstance(source, str) and bool(source) and source in chunk
        target_in_chunk = isinstance(target, str) and bool(target) and target in chunk
        relation_mentioned = False
        if (
            source_in_chunk
            and target_in_chunk
        ):
            return True
        for key in ("relation", "type", "label", "description"):
            value = edge.get(key)
            if isinstance(value, str) and value and value in chunk:
                relation_mentioned = True
                if source_in_chunk or target_in_chunk:
                    return True
        chunk_tokens = _tokenize(chunk, explanation_profile)
        if not support_terms or not chunk_tokens:
            return False
        if not (target_in_chunk or relation_mentioned):
            return False
        return (
            _normalized_overlap(chunk_tokens, support_terms) >= 0.35
            or len(chunk_tokens & support_terms) >= 2
        )

    def _select_evidence(
        self,
        path: dict[str, Any],
        evidence_chunks: list[str],
        limit: int = 2,
        explanation_profile: Mapping[str, Any] | None = None,
        evidence_policy: Mapping[str, Any] | None = None,
    ) -> EvidenceSelection:
        path_signals = self._build_path_signals(path, explanation_profile)
        query_tokens = _tokenize(
            " ".join(
                [
                    path.get("path_text", ""),
                    *path_signals["node_phrases"],
                    *path_signals["edge_phrases"],
                ]
            ),
            explanation_profile,
        )
        scored: list[tuple[float, str]] = []
        for chunk in evidence_chunks:
            score = self._score_evidence_chunk(
                query_tokens=query_tokens,
                path_signals=path_signals,
                chunk=chunk,
                explanation_profile=explanation_profile,
            )
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        policy_id = (
            str(evidence_policy.get("policy_id") or "").strip()
            if isinstance(evidence_policy, Mapping)
            else None
        )
        if not scored or scored[0][0] < EVIDENCE_SCORE_THRESHOLD:
            if not _policy_requires_evidence(evidence_policy):
                return EvidenceSelection(
                    policy_id=policy_id,
                    fully_supported=False,
                )
            return EvidenceSelection(policy_id=policy_id)

        min_supporting_chunks = _policy_min_supporting_chunks(evidence_policy)
        selection_limit = max(limit, min_supporting_chunks)
        if _policy_requires_per_hop_support(evidence_policy):
            selection_limit = max(
                selection_limit,
                len([edge for edge in path.get("edges", []) if isinstance(edge, dict)]),
            )
        selected = scored[:selection_limit]
        selected_chunks = [chunk for _, chunk in selected]
        supporting_chunk_count = sum(
            1 for score, _ in scored if score >= EVIDENCE_SCORE_THRESHOLD
        )

        total_hops = len([edge for edge in path.get("edges", []) if isinstance(edge, dict)])
        if total_hops <= 0:
            total_hops = max(0, len(path.get("nodes", [])) - 1)
        matched_hops = total_hops
        fully_supported = supporting_chunk_count >= min_supporting_chunks

        if _policy_requires_per_hop_support(evidence_policy):
            matched_hops = self._count_supported_hops(
                path=path,
                chunks=selected_chunks,
                path_signals=path_signals,
                explanation_profile=explanation_profile,
            )
            if total_hops > 0:
                fully_supported = fully_supported and matched_hops >= total_hops

        if not fully_supported and not _policy_allows_partial_support(evidence_policy):
            return EvidenceSelection(
                policy_id=policy_id,
                supporting_chunk_count=supporting_chunk_count,
                fully_supported=False,
                matched_hops=matched_hops,
                total_hops=total_hops,
            )

        return EvidenceSelection(
            evidence=selected_chunks,
            supporting_chunk_count=supporting_chunk_count,
            fully_supported=fully_supported,
            matched_hops=matched_hops,
            total_hops=total_hops,
            policy_id=policy_id,
        )

    def _build_path_signals(
        self,
        path: dict[str, Any],
        explanation_profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        path_tokens = _tokenize(path.get("path_text", ""), explanation_profile)
        node_tokens: set[str] = set()
        edge_tokens: set[str] = set()
        relation_types: set[str] = set()
        relation_tags: set[str] = set()
        node_phrases: list[str] = []
        edge_phrases: list[str] = []
        matched_relation_rules: list[dict[str, Any]] = []
        edge_support_terms: list[set[str]] = []
        node_types: list[str] = []
        direction_match_count = 0
        direction_mismatch_count = 0
        node_type_lookup: dict[str, str] = {}
        seen_node_ids: dict[str, int] = {}

        for node in path.get("nodes", []):
            if not isinstance(node, dict):
                continue
            node_identifier = _extract_node_identifier(node)
            node_type = _extract_node_type(node)
            if node_type:
                node_types.append(node_type)
            if node_identifier:
                normalized_identifier = _normalize_match_text(node_identifier)
                if normalized_identifier:
                    seen_node_ids[normalized_identifier] = (
                        seen_node_ids.get(normalized_identifier, 0) + 1
                    )
                if node_type:
                    for key in ("id", "name", "entity", "entity_name", "label"):
                        value = node.get(key)
                        if isinstance(value, str) and value.strip():
                            node_type_lookup[value.strip()] = node_type
            for key in ("id", "name", "label", "entity", "entity_name"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    node_phrases.append(value)
                    node_tokens.update(_tokenize(value, explanation_profile))

        for edge in path.get("edges", []):
            if not isinstance(edge, dict):
                continue
            edge_terms: set[str] = set()
            relation_rules = _match_relation_semantic_rules(edge, explanation_profile)
            matched_relation_rules.extend(relation_rules)
            edge_source_type = (
                str(edge.get("source_type") or edge.get("source_entity_type") or "").strip()
                or node_type_lookup.get(str(edge.get("source") or "").strip(), "")
            )
            edge_target_type = (
                str(edge.get("target_type") or edge.get("target_entity_type") or "").strip()
                or node_type_lookup.get(str(edge.get("target") or "").strip(), "")
            )
            for rule in relation_rules:
                relation_type = str(rule.get("relation_type") or "").strip()
                if relation_type:
                    relation_types.add(relation_type)
                for semantic_tag in list(rule.get("semantic_tags") or []):
                    tag_token = _semantic_tag_token(semantic_tag)
                    if tag_token:
                        relation_tags.add(tag_token)
                        edge_terms.add(tag_token)
                rule_source_types = {
                    _normalize_type_key(item)
                    for item in list(rule.get("source_types") or [])
                    if _normalize_type_key(item)
                }
                rule_target_types = {
                    _normalize_type_key(item)
                    for item in list(rule.get("target_types") or [])
                    if _normalize_type_key(item)
                }
                normalized_source_type = _normalize_type_key(edge_source_type)
                normalized_target_type = _normalize_type_key(edge_target_type)
                if (
                    rule_source_types
                    and rule_target_types
                    and normalized_source_type
                    and normalized_target_type
                ):
                    direction = str(rule.get("direction") or "undirected").strip()
                    forward_match = (
                        normalized_source_type in rule_source_types
                        and normalized_target_type in rule_target_types
                    )
                    reverse_match = (
                        normalized_source_type in rule_target_types
                        and normalized_target_type in rule_source_types
                    )
                    if direction == "target_to_source":
                        forward_match, reverse_match = reverse_match, forward_match
                    if direction == "undirected":
                        if forward_match or reverse_match:
                            direction_match_count += 1
                        else:
                            direction_mismatch_count += 1
                    elif forward_match:
                        direction_match_count += 1
                    else:
                        direction_mismatch_count += 1
            for key in (
                "relation",
                "type",
                "label",
                "description",
                "source",
                "target",
            ):
                value = edge.get(key)
                if isinstance(value, str) and value.strip():
                    edge_phrases.append(value)
                    tokenized = _tokenize(value, explanation_profile)
                    edge_tokens.update(tokenized)
                    edge_terms.update(tokenized)
            edge_support_terms.append(edge_terms)

        path_tokens.update(node_tokens)
        path_tokens.update(edge_tokens)
        repeated_node_count = sum(max(0, count - 1) for count in seen_node_ids.values())
        return {
            "path_tokens": path_tokens,
            "node_tokens": node_tokens,
            "edge_tokens": edge_tokens,
            "relation_types": relation_types,
            "relation_tags": relation_tags,
            "node_types": node_types,
            "node_phrases": node_phrases,
            "edge_phrases": edge_phrases,
            "matched_relation_rules": matched_relation_rules,
            "edge_support_terms": edge_support_terms,
            "direction_match_count": direction_match_count,
            "direction_mismatch_count": direction_mismatch_count,
            "repeated_node_count": repeated_node_count,
        }

    def _score_evidence_chunk(
        self,
        *,
        query_tokens: set[str],
        path_signals: dict[str, Any],
        chunk: str,
        explanation_profile: Mapping[str, Any] | None = None,
    ) -> float:
        chunk_tokens = _tokenize(chunk, explanation_profile)
        if not chunk_tokens:
            return 0.0
        node_mentions = sum(
            1 for phrase in path_signals["node_phrases"] if phrase and phrase in chunk
        )
        edge_mentions = sum(
            1 for phrase in path_signals["edge_phrases"] if phrase and phrase in chunk
        )
        return (
            _normalized_overlap(chunk_tokens, path_signals["path_tokens"]) * 8.0
            + _normalized_overlap(chunk_tokens, query_tokens) * 5.0
            + node_mentions * 2.0
            + edge_mentions * 1.0
        )

    async def _build_final_explanation(
        self,
        *,
        query: str,
        explained_path: ExplainedPath,
        domain_schema: dict[str, Any] | None,
        explanation_profile: Mapping[str, Any] | None,
        intent_family: str | None,
        template_id: str | None,
        scenario_id: str | None,
        scenario_override: Mapping[str, Any] | None,
        evidence_policy: Mapping[str, Any] | None,
        output_contract: Mapping[str, Any] | None,
        guardrails: Mapping[str, Any] | None,
        support_is_partial: bool,
    ) -> str:
        if self.llm_client is not None and self.llm_client.is_available():
            system_prompt, user_prompt = build_path_explainer_prompt(
                query=query,
                graph_paths=[
                    {
                        "path_text": explained_path.path_text,
                        "nodes": explained_path.nodes,
                        "edges": explained_path.edges,
                    }
                ],
                evidence_chunks=explained_path.evidence,
                domain_schema=domain_schema,
                explanation_profile=explanation_profile,
                intent_family=intent_family,
                template_id=template_id,
                scenario_id=scenario_id,
                scenario_override=dict(scenario_override or {}),
                evidence_policy=dict(evidence_policy or {}),
                output_contract=dict(output_contract or {}),
                guardrails=dict(guardrails or {}),
            )
            try:
                payload = await self.llm_client.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=500,
                )
                explanation = str(payload.get("final_explanation", "")).strip()
                if explanation:
                    return explanation
            except Exception:
                pass

        evidence_preview = " ".join(explained_path.evidence[:2])
        output_sections = {
            str(section).strip()
            for section in list((output_contract or {}).get("required_sections") or [])
            if str(section).strip()
        }
        hedge = (
            "suggests"
            if support_is_partial
            or _guardrail_flag(guardrails, "disallow_metric_claim_without_support")
            else "shows"
        )
        if "causal_chain" in output_sections:
            return (
                f"The best-supported causal chain {hedge} that '{explained_path.path_text}' "
                f"is grounded in the retrieved evidence: {evidence_preview}"
            )
        if "source_chain" in output_sections or "document_chain" in output_sections:
            return (
                f"The best-supported source chain {hedge} that '{explained_path.path_text}' "
                f"can be traced in the retrieved evidence: {evidence_preview}"
            )
        return (
            f"Based on the graph path '{explained_path.path_text}', "
            f"the strongest supported explanation {hedge} that the linked entities and relations "
            f"are reflected in the retrieved evidence: {evidence_preview}"
        )

    def _build_uncertainty(
        self,
        *,
        query: str,
        explained_path: ExplainedPath,
        evidence_selection: EvidenceSelection,
        explanation_profile: Mapping[str, Any] | None,
        intent_binding: Mapping[str, Any] | None,
        evidence_policy: Mapping[str, Any] | None,
        output_contract: Mapping[str, Any] | None,
        guardrails: Mapping[str, Any] | None,
    ) -> str | None:
        messages: list[str] = []
        if not evidence_selection.evidence and not _policy_requires_evidence(evidence_policy):
            messages.append("No supporting evidence chunk was retrieved for this best-effort explanation.")

        if (
            not evidence_selection.fully_supported
            and evidence_selection.evidence
            and (
                _policy_allows_partial_support(evidence_policy)
                or _contract_requires_uncertainty(output_contract)
                or _guardrail_flag(
                    guardrails,
                    "require_explicit_uncertainty_on_partial_support",
                )
            )
        ):
            messages.append("The explanation is only partially supported by the retrieved evidence.")

        if (
            _guardrail_flag(guardrails, "disallow_metric_claim_without_support")
            and not evidence_selection.fully_supported
            and self._query_targets_metric(
                query=query,
                explanation_profile=explanation_profile,
                intent_binding=intent_binding,
            )
        ):
            messages.append(
                "Metric-related impact should be treated as tentative because the supporting evidence is incomplete."
            )

        if (
            explained_path.confidence < 0.7
            and not messages
        ):
            messages.append(
                "Evidence is limited to the retrieved chunks and may require external verification."
            )

        return " ".join(messages) or None

    @staticmethod
    def _query_targets_metric(
        *,
        query: str,
        explanation_profile: Mapping[str, Any] | None,
        intent_binding: Mapping[str, Any] | None,
    ) -> bool:
        metric_tag = _semantic_tag_token("metric")
        query_tags = _extract_semantic_tags(query, explanation_profile)
        if metric_tag in query_tags:
            return True
        if not isinstance(intent_binding, Mapping):
            return False
        preferred_tags = {
            _semantic_tag_token(value)
            for value in list(intent_binding.get("preferred_semantic_tags") or [])
        }
        return metric_tag in preferred_tags

    @staticmethod
    def _build_fallback_result(
        query: str,
        intent_family: str | None = None,
    ) -> PathExplanation:
        question_type = (
            _map_intent_family_to_question_type(intent_family)
            if intent_family
            else _legacy_question_type(query)
        )
        return PathExplanation(
            enabled=False,
            question_type=question_type,
            core_entities=[],
            paths=[],
            final_explanation="",
            uncertainty="No graph path with sufficient supporting evidence was found.",
        )
