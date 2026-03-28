from __future__ import annotations

from lightrag_fork.constants import DEFAULT_ENTITY_TYPES, DEFAULT_SUMMARY_LANGUAGE
from lightrag_fork.schema import DomainSchema, EntityTypeDefinition


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
    aliases={},
    extraction_rules=[],
    metadata={"builtin": True},
)
