from pathlib import Path

from kg_agent.skills import SkillLoader, SkillRegistry


def test_skill_registry_scans_repo_skills_directory():
    registry = SkillRegistry(Path("skills"))

    skills = registry.list_skills()
    names = [item.name for item in skills]

    assert "example-skill" in names
    assert "xlsx" in names


def test_skill_registry_supports_official_style_skill_without_explicit_entrypoint(tmp_path):
    skill_dir = tmp_path / "skills" / "official-doc-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Official Doc Skill\n\n"
        "Use this skill when the user needs a documented workflow.\n\n"
        "1. Read the docs.\n"
        "2. Inspect references.\n"
        "3. Execute the needed steps.\n",
        encoding="utf-8",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "GUIDE.md").write_text(
        "Reference content",
        encoding="utf-8",
    )

    registry = SkillRegistry(tmp_path / "skills")
    loader = SkillLoader(registry)

    skill = registry.get("Official Doc Skill")
    loaded = loader.load_skill("Official Doc Skill")

    assert skill is not None
    assert "documented workflow" in skill.description
    assert loaded.skill.name == "Official Doc Skill"
    assert any(item.kind == "reference" for item in loaded.file_inventory)
