from __future__ import annotations

from pathlib import Path
from typing import Any

from kg_agent.skills.models import SkillDefinition

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {}, raw
    metadata: dict[str, Any] = {}
    if yaml is not None:
        parsed = yaml.safe_load(raw[4:end]) or {}
        if isinstance(parsed, dict):
            metadata = parsed
    body = raw[end + len("\n---\n") :]
    return metadata, body


def _extract_first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _extract_description(metadata: dict[str, Any], body: str) -> str:
    for key in ("description", "summary"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    paragraph: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    if paragraph:
        return " ".join(paragraph)
    return "No description provided."


def _extract_tags(metadata: dict[str, Any]) -> list[str]:
    raw_tags = metadata.get("tags")
    if not isinstance(raw_tags, list):
        return []
    return [str(tag).strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()]


class SkillRegistry:
    def __init__(self, skills_root: str | Path = "skills"):
        self.skills_root = Path(skills_root).resolve()
        self._skills: dict[str, SkillDefinition] = {}
        self.refresh()

    def refresh(self) -> list[SkillDefinition]:
        skills: dict[str, SkillDefinition] = {}
        if self.skills_root.exists():
            for skill_dir in sorted(path for path in self.skills_root.iterdir() if path.is_dir()):
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue
                raw = _read_text(skill_md)
                metadata, body = _split_frontmatter(raw)
                name = str(metadata.get("name", "")).strip() or _extract_first_heading(body) or skill_dir.name
                skills[name] = SkillDefinition(
                    name=name,
                    description=_extract_description(metadata, body),
                    path=skill_dir.resolve(),
                    tags=_extract_tags(metadata),
                    metadata=metadata,
                )
        self._skills = skills
        return self.list_skills()

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def has(self, name: str) -> bool:
        return name in self._skills

    def list_skills(self) -> list[SkillDefinition]:
        return [self._skills[name] for name in sorted(self._skills)]
