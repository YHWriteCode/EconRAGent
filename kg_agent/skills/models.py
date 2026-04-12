from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    path: Path
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_catalog_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "path": str(self.path),
        }


@dataclass(frozen=True)
class SkillPlan:
    skill_name: str
    goal: str
    reason: str
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillFileEntry:
    path: str
    kind: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class LoadedSkill:
    skill: SkillDefinition
    skill_md: str
    file_inventory: list[SkillFileEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill.to_catalog_dict(),
            "skill_md": self.skill_md,
            "file_inventory": [item.to_dict() for item in self.file_inventory],
        }


@dataclass(frozen=True)
class SkillExecutionRequest:
    skill_name: str
    goal: str
    user_query: str
    workspace: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
