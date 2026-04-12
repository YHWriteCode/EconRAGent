from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent


SKILLS_ROOT = Path(os.environ.get("MCP_SKILLS_DIR", "/app/skills")).resolve()
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_DIR", "/workspace")).resolve()
DEFAULT_SCRIPT_TIMEOUT_S = int(os.environ.get("MCP_SCRIPT_TIMEOUT_S", "120"))
MAX_REFERENCE_BYTES = int(os.environ.get("MCP_MAX_REFERENCE_BYTES", "200000"))

mcp = FastMCP("SkillRuntimeService", json_response=True)


class SkillServerError(RuntimeError):
    pass


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
    skill_dir = (SKILLS_ROOT / skill_name.strip()).resolve()
    if skill_dir.parent != SKILLS_ROOT or not skill_dir.is_dir():
        raise SkillServerError(f"Unknown skill: {skill_name}")
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SkillServerError(f"Skill is missing SKILL.md: {skill_name}")
    return skill_dir


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


def _compat_default_script_name(metadata: dict[str, Any], scripts: list[str]) -> str | None:
    for key in ("default_script", "recommended_script", "entrypoint", "script"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return scripts[0] if scripts else None


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


def _build_skill_catalog_entry(skill_dir: Path) -> dict[str, Any]:
    metadata, body = _parse_skill_markdown(skill_dir / "SKILL.md")
    summary = _extract_summary(metadata, body)
    tags = _extract_skill_tags(metadata)
    files = _iter_skill_files(skill_dir)
    scripts = [
        str(path.relative_to(skill_dir)).replace("\\", "/")
        for path in files
        if _classify_skill_file(skill_dir, path) == "script"
    ]
    return {
        "name": skill_dir.name,
        "description": summary,
        "tags": tags,
        "path": str(skill_dir),
        "script_count": len(scripts),
        "default_script": _compat_default_script_name(metadata, scripts),
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
        "default_script": _compat_default_script_name(metadata, scripts),
        "file_inventory": file_inventory,
    }


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


def _build_script_env(skill_dir: Path, workspace_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "SKILL_NAME": skill_dir.name,
            "SKILL_ROOT": str(skill_dir),
            "SKILL_WORKSPACE": str(workspace_dir),
            "HOME": str(workspace_dir),
        }
    )
    return env


@mcp.tool()
def list_skills(query: str | None = None) -> dict[str, Any]:
    """Return the lightweight local skill catalog."""
    skills = [_build_skill_catalog_entry(skill_dir) for skill_dir in _skill_dirs()]
    selected_skill = None
    if query:
        ranked = sorted(
            (
                (
                    _score_skill_match(
                        query,
                        name=item["name"],
                        summary=item["description"],
                        tags=item["tags"],
                    ),
                    item,
                )
                for item in skills
            ),
            key=lambda item: (item[0], item[1]["name"]),
            reverse=True,
        )
        if ranked and ranked[0][0] > 0:
            selected_skill = ranked[0][1]
    return {
        "summary": f"Discovered {len(skills)} skill(s)",
        "skills": skills,
        "count": len(skills),
        "query": query or "",
        "selected_skill": selected_skill,
        "selected_skill_name": (
            selected_skill["name"] if isinstance(selected_skill, dict) else None
        ),
    }


@mcp.tool()
def read_skill(skill_name: str) -> dict[str, Any]:
    """Load SKILL.md and the indexed file inventory for one skill."""
    payload = _load_skill_payload(skill_name)
    return {
        "summary": f"Loaded skill '{payload['name']}'",
        "name": payload["name"],
        "description": payload["description"],
        "tags": payload["tags"],
        "path": payload["path"],
        "skill": {
            "name": payload["name"],
            "description": payload["description"],
            "tags": payload["tags"],
            "path": payload["path"],
        },
        "skill_md": payload["skill_md"],
        "file_inventory": payload["file_inventory"],
        "references": payload["references"],
        "scripts": payload["scripts"],
        "default_script": payload["default_script"],
    }


@mcp.tool()
def read_skill_file(skill_name: str, relative_path: str) -> dict[str, Any]:
    """Read one file from a skill directory by relative path."""
    skill_dir = _resolve_skill_dir(skill_name)
    target = _resolve_skill_file_path(skill_dir, relative_path)
    content = _read_text(target)
    byte_length = len(content.encode("utf-8"))
    truncated = False
    if byte_length > MAX_REFERENCE_BYTES:
        content = content.encode("utf-8")[:MAX_REFERENCE_BYTES].decode(
            "utf-8", errors="ignore"
        )
        truncated = True
    return {
        "summary": f"Read skill file '{relative_path}' from '{skill_name}'",
        "skill_name": skill_name,
        "path": str(target.relative_to(skill_dir)).replace("\\", "/"),
        "kind": _classify_skill_file(skill_dir, target),
        "content": content,
        "truncated": truncated,
    }


@mcp.tool()
def read_skill_docs(skill_name: str) -> dict[str, Any]:
    """Compatibility wrapper around the new coarse-grained read_skill interface."""
    payload = read_skill(skill_name)
    payload["summary"] = f"Loaded docs for skill '{skill_name}'"
    return payload


@mcp.tool()
async def run_skill_task(
    skill_name: str,
    goal: str,
    user_query: str | None = None,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare a skill task for runtime execution without exposing scripts as planner tools."""
    payload = _load_skill_payload(skill_name)
    return {
        "summary": f"Prepared skill task for '{skill_name}'",
        "status": "prepared",
        "name": payload["name"],
        "skill_name": skill_name,
        "goal": goal,
        "user_query": user_query or "",
        "constraints": constraints or {},
        "skill": {
            "name": payload["name"],
            "description": payload["description"],
            "tags": payload["tags"],
            "path": payload["path"],
        },
        "file_inventory": payload["file_inventory"],
        "references": payload["references"],
        "scripts": payload["scripts"],
        "default_script": payload["default_script"],
        "notes": (
            "This runtime service prepares coarse-grained skill context. "
            "Script-level execution remains a legacy compatibility path."
        ),
    }


@mcp.tool()
async def execute_skill_script(
    skill_name: str,
    script_name: str,
    args: list[str] | None = None,
    timeout_s: int = DEFAULT_SCRIPT_TIMEOUT_S,
    cleanup_workspace: bool = False,
) -> CallToolResult:
    """Legacy compatibility wrapper for explicit script execution."""
    skill_dir = _resolve_skill_dir(skill_name)
    script_path = _resolve_script_path(skill_dir, script_name)
    workspace_dir = Path(
        tempfile.mkdtemp(prefix=f"{skill_dir.name}-", dir=str(WORKSPACE_ROOT))
    ).resolve()
    command = _build_command(script_path, [str(item) for item in (args or [])])

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace_dir),
            env=_build_script_env(skill_dir, workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, int(timeout_s)),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SkillServerError(
                f"Script timed out after {timeout_s} seconds: {script_name}"
            ) from exc

        produced_files = sorted(
            str(path.relative_to(workspace_dir)).replace("\\", "/")
            for path in workspace_dir.rglob("*")
            if path.is_file()
        )
        result_payload = {
            "skill_name": skill_dir.name,
            "script_name": str(script_path.relative_to(skill_dir)).replace("\\", "/"),
            "command": command,
            "workspace": str(workspace_dir),
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "produced_files": produced_files,
            "success": process.returncode == 0,
            "summary": (
                f"Executed {skill_dir.name}/{script_path.name} "
                f"with exit code {process.returncode}"
            ),
        }
        text = result_payload["summary"]
        if result_payload["stderr"]:
            text = f"{text}\n\nstderr:\n{result_payload['stderr']}"
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=result_payload,
            isError=process.returncode != 0,
        )
    finally:
        if cleanup_workspace:
            shutil.rmtree(workspace_dir, ignore_errors=True)


@mcp.resource("skill://catalog")
def skill_catalog_resource() -> str:
    """Read-only skill catalog resource for clients that support resources/list/read."""
    return json.dumps(list_skills(), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}")
def skill_resource(skill_name: str) -> str:
    """Read-only resource exposing one skill's coarse-grained payload."""
    return json.dumps(read_skill(skill_name), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}/docs")
def skill_docs_resource(skill_name: str) -> str:
    """Compatibility resource exposing one skill's docs payload."""
    return json.dumps(read_skill_docs(skill_name), ensure_ascii=False, indent=2)


@mcp.resource("skill://{skill_name}/files/{relative_path}")
def skill_file_resource(skill_name: str, relative_path: str) -> str:
    """Read one skill file by relative path."""
    return json.dumps(
        read_skill_file(skill_name, relative_path),
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("skill://{skill_name}/references/{reference_name}")
def skill_reference_resource(skill_name: str, reference_name: str) -> str:
    """Read one reference file by relative path."""
    skill_dir = _resolve_skill_dir(skill_name)
    references_dir = (skill_dir / "references").resolve()
    if not references_dir.is_dir():
        raise SkillServerError(f"Skill has no references directory: {skill_name}")
    ref_path = (references_dir / reference_name).resolve()
    if ref_path == references_dir or references_dir not in ref_path.parents:
        raise SkillServerError("reference_name must resolve inside references/")
    if not ref_path.is_file():
        raise SkillServerError(f"Unknown reference: {reference_name}")
    return _read_text(ref_path)


def main() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
