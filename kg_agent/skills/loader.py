from __future__ import annotations

from pathlib import Path

from kg_agent.skills.models import LoadedSkill, SkillFileEntry
from kg_agent.skills.registry import SkillRegistry


class SkillLoader:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def load_skill(self, skill_name: str) -> LoadedSkill:
        skill = self.registry.get(skill_name)
        if skill is None:
            raise LookupError(f"Skill is not registered: {skill_name}")
        skill_md_path = skill.path / "SKILL.md"
        return LoadedSkill(
            skill=skill,
            skill_md=skill_md_path.read_text(encoding="utf-8"),
            file_inventory=self.list_skill_files(skill_name),
        )

    def list_skill_files(self, skill_name: str) -> list[SkillFileEntry]:
        skill = self.registry.get(skill_name)
        if skill is None:
            raise LookupError(f"Skill is not registered: {skill_name}")
        entries: list[SkillFileEntry] = []
        for path in sorted(skill.path.rglob("*")):
            if not path.is_file():
                continue
            relative_path = str(path.relative_to(skill.path)).replace("\\", "/")
            entries.append(
                SkillFileEntry(
                    path=relative_path,
                    kind=self.classify_path(relative_path),
                    size_bytes=path.stat().st_size,
                )
            )
        return entries

    def read_skill_file(self, skill_name: str, relative_path: str) -> str:
        skill = self.registry.get(skill_name)
        if skill is None:
            raise LookupError(f"Skill is not registered: {skill_name}")
        target = (skill.path / relative_path).resolve()
        if skill.path not in target.parents and target != skill.path:
            raise ValueError("relative_path must stay inside the skill directory")
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        return target.read_text(encoding="utf-8")

    @staticmethod
    def classify_path(relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized == "SKILL.md":
            return "skill_doc"
        if normalized.startswith("references/"):
            return "reference"
        if normalized.startswith("scripts/"):
            return "script"
        if normalized.startswith("assets/"):
            return "asset"
        if normalized.endswith(".md"):
            return "markdown"
        return "other"
