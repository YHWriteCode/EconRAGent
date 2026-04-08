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
class DomainSchema:
    domain_name: str
    profile_name: str
    enabled: bool = DEFAULT_DOMAIN_SCHEMA_ENABLED
    mode: str = DEFAULT_DOMAIN_SCHEMA_MODE
    language: str = DEFAULT_SUMMARY_LANGUAGE
    description: str = ""
    entity_types: list[EntityTypeDefinition] = field(default_factory=list)
    relation_types: list[RelationTypeDefinition] = field(default_factory=list)
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
            "aliases": deepcopy(self.aliases),
            "extraction_rules": list(self.extraction_rules),
            "metadata": deepcopy(self.metadata),
        }


def _normalize_schema_match_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value.strip().lower() if char.isalnum())


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
    return normalized or entity_type


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

    profile_name = str(
        config.get("profile_name") or DEFAULT_DOMAIN_SCHEMA_PROFILE
    ).strip() or DEFAULT_DOMAIN_SCHEMA_PROFILE
    if profile_name not in registry:
        valid_names = ", ".join(sorted(registry))
        raise ValueError(
            f"Unknown domain schema profile '{profile_name}'. Valid profiles: {valid_names}"
        )

    base_schema = registry[profile_name]
    resolved = replace(
        base_schema,
        enabled=bool(config.get("enabled", base_schema.enabled)),
        mode=str(config.get("mode", base_schema.mode) or base_schema.mode),
        profile_name=profile_name,
        domain_name=str(config.get("domain_name", base_schema.domain_name) or base_schema.domain_name),
        language=str(config.get("language", base_schema.language) or base_schema.language),
        description=str(config.get("description", base_schema.description) or base_schema.description),
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
