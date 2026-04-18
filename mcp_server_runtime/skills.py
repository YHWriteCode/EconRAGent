from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from kg_agent.skills.command_planner import build_shell_hints as build_skill_shell_hints
from kg_agent.skills.models import LoadedSkill, SkillDefinition, SkillFileEntry

from .config import MAX_REFERENCE_BYTES, SKILLS_ROOT
from .errors import SkillServerError


def _skill_dirs() -> list[Path]:
    if not SKILLS_ROOT.exists():
        return []
    return sorted(
        path
        for path in SKILLS_ROOT.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )


def _resolve_skill_dir(skill_name: str) -> Path:
    if not skill_name or not skill_name.strip():
        raise SkillServerError("skill_name is required")
    requested = skill_name.strip()
    skill_dir = (SKILLS_ROOT / requested).resolve()
    if (
        skill_dir.parent == SKILLS_ROOT
        and skill_dir.is_dir()
        and (skill_dir / "SKILL.md").is_file()
    ):
        return skill_dir

    for candidate in _skill_dirs():
        metadata, body = _parse_skill_markdown(candidate / "SKILL.md")
        aliases = {
            candidate.name,
            str(metadata.get("name", "")).strip(),
            _extract_first_heading(body),
            str(candidate.resolve()),
        }
        aliases.discard("")
        if requested in aliases:
            return candidate
    raise SkillServerError(f"Unknown skill: {skill_name}")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_skill_markdown(skill_md_path: Path) -> tuple[dict[str, Any], str]:
    raw = _read_text(skill_md_path)
    metadata: dict[str, Any] = {}
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            frontmatter = raw[4:end]
            parsed = yaml.safe_load(frontmatter) or {}
            if isinstance(parsed, dict):
                metadata = parsed
            raw = raw[end + len("\n---\n") :]
    return metadata, raw.strip()


def _extract_first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _extract_summary(metadata: dict[str, Any], body: str) -> str:
    summary = metadata.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

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
    return "No summary available."


def _extract_skill_tags(metadata: dict[str, Any]) -> list[str]:
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag).strip() for tag in tags if isinstance(tag, str) and tag.strip()]


def _iter_skill_files(skill_dir: Path) -> list[Path]:
    return sorted(path for path in skill_dir.rglob("*") if path.is_file())


def _classify_skill_file(skill_dir: Path, path: Path) -> str:
    relative_path = str(path.relative_to(skill_dir)).replace("\\", "/")
    if relative_path == "SKILL.md":
        return "skill_doc"
    if relative_path.startswith("references/"):
        return "reference"
    if relative_path.startswith("scripts/"):
        return "script"
    if relative_path.startswith("assets/"):
        return "asset"
    if relative_path.endswith(".md"):
        return "markdown"
    return "other"


def _iter_runnable_scripts(skill_dir: Path) -> list[str]:
    runnable: list[str] = []
    for path in _iter_skill_files(skill_dir):
        relative_path = str(path.relative_to(skill_dir)).replace("\\", "/")
        if _classify_skill_file(skill_dir, path) != "script":
            continue
        suffix = path.suffix.lower()
        if suffix in {".py", ".sh", ".bash", ".ps1"} or os.access(path, os.X_OK):
            runnable.append(relative_path)
    return runnable


def _build_skill_catalog_entry(skill_dir: Path) -> dict[str, Any]:
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    summary = _extract_summary(metadata, body)
    tags = _extract_skill_tags(metadata)
    shell_hints = build_skill_shell_hints(_build_loaded_skill_from_dir(skill_dir))
    return {
        "name": skill_dir.name,
        "description": summary,
        "tags": tags,
        "path": str(skill_dir),
        "execution_mode": "shell",
        "shell_example_count": len(shell_hints["example_commands"]),
        "runnable_script_count": len(shell_hints["runnable_scripts"]),
    }


def _score_skill_match(query: str, *, name: str, summary: str, tags: list[str]) -> int:
    query_lower = (query or "").strip().lower()
    if not query_lower:
        return 0
    parts = [name, summary, " ".join(tags)]
    search_text = " ".join(parts).replace("_", " ").replace("-", " ").lower()
    score = 0
    if name and name.lower() in query_lower:
        score += 5
    for token in re.findall(r"[a-z][a-z0-9_/-]{2,}", query_lower):
        normalized = token.strip("_-/")
        if normalized and normalized in search_text:
            score += 2
    for tag in tags:
        if len(tag) >= 3 and tag.lower() in query_lower:
            score += 3
    return score


def _iter_reference_files(skill_dir: Path) -> list[Path]:
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        return []
    return sorted(path for path in references_dir.rglob("*") if path.is_file())


def _load_skill_payload(skill_name: str) -> dict[str, Any]:
    skill_dir = _resolve_skill_dir(skill_name)
    raw_skill_md = _read_text(skill_dir / "SKILL.md")
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    summary = _extract_summary(metadata, body)
    references: list[dict[str, Any]] = []
    for ref_path in _iter_reference_files(skill_dir):
        content = _read_text(ref_path)
        byte_length = len(content.encode("utf-8"))
        truncated = False
        if byte_length > MAX_REFERENCE_BYTES:
            content = content.encode("utf-8")[:MAX_REFERENCE_BYTES].decode(
                "utf-8", errors="ignore"
            )
            truncated = True
        references.append(
            {
                "path": str(ref_path.relative_to(skill_dir)).replace("\\", "/"),
                "content": content,
                "truncated": truncated,
            }
        )

    scripts_dir = skill_dir / "scripts"
    scripts = []
    if scripts_dir.is_dir():
        scripts = sorted(
            str(path.relative_to(skill_dir)).replace("\\", "/")
            for path in scripts_dir.rglob("*")
            if path.is_file()
        )

    file_inventory = [
        {
            "path": str(path.relative_to(skill_dir)).replace("\\", "/"),
            "kind": _classify_skill_file(skill_dir, path),
            "size_bytes": path.stat().st_size,
        }
        for path in _iter_skill_files(skill_dir)
    ]
    shell_hints = build_skill_shell_hints(_build_loaded_skill_from_dir(skill_dir))

    return {
        "name": skill_dir.name,
        "summary": summary,
        "description": summary,
        "tags": _extract_skill_tags(metadata),
        "path": str(skill_dir),
        "metadata": metadata,
        "skill_md": raw_skill_md,
        "skill_body": body,
        "references": references,
        "scripts": scripts,
        "shell_hints": shell_hints,
        "file_inventory": file_inventory,
    }


def _build_loaded_skill(skill_name: str) -> LoadedSkill:
    skill_dir = _resolve_skill_dir(skill_name)
    return _build_loaded_skill_from_dir(skill_dir)


def _build_loaded_skill_from_dir(skill_dir: Path) -> LoadedSkill:
    raw_skill_md = _read_text(skill_dir / "SKILL.md")
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    name = (
        str(metadata.get("name", "")).strip()
        or _extract_first_heading(body)
        or skill_dir.name
    )
    file_inventory = [
        SkillFileEntry(
            path=str(path.relative_to(skill_dir)).replace("\\", "/"),
            kind=_classify_skill_file(skill_dir, path),
            size_bytes=path.stat().st_size,
        )
        for path in _iter_skill_files(skill_dir)
    ]
    return LoadedSkill(
        skill=SkillDefinition(
            name=name,
            description=_extract_summary(metadata, body),
            path=skill_dir.resolve(),
            tags=_extract_skill_tags(metadata),
            metadata=metadata,
        ),
        skill_md=raw_skill_md,
        file_inventory=file_inventory,
    )


def _resolve_script_path(skill_dir: Path, script_name: str) -> Path:
    scripts_dir = (skill_dir / "scripts").resolve()
    if not scripts_dir.is_dir():
        raise SkillServerError(f"Skill has no scripts directory: {skill_dir.name}")
    script_path = (scripts_dir / script_name.strip()).resolve()
    if script_path == scripts_dir or scripts_dir not in script_path.parents:
        raise SkillServerError("script_name must resolve inside the skill scripts directory")
    if not script_path.is_file():
        raise SkillServerError(f"Unknown script: {script_name}")
    return script_path


def _resolve_skill_file_path(skill_dir: Path, relative_path: str) -> Path:
    if not relative_path or not relative_path.strip():
        raise SkillServerError("relative_path is required")
    target = (skill_dir / relative_path.strip()).resolve()
    if target != skill_dir and skill_dir not in target.parents:
        raise SkillServerError("relative_path must resolve inside the skill directory")
    if not target.is_file():
        raise SkillServerError(f"Unknown skill file: {relative_path}")
    return target


def _build_command(script_path: Path, args: list[str]) -> list[str]:
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        return ["python", str(script_path), *args]
    if suffix in {".sh", ".bash"}:
        return ["/bin/sh", str(script_path), *args]
    if os.access(script_path, os.X_OK):
        return [str(script_path), *args]
    raise SkillServerError(
        "Unsupported script type. Use a .py, .sh, .bash, or executable file."
    )
