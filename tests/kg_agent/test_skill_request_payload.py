from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path


def _load_runtime_service_module(*, tmp_path: Path, monkeypatch) -> object:
    skills_root = Path("skills").resolve()
    workspace_root = (tmp_path / "runtime-workspace").resolve()
    db_path = (tmp_path / "runtime-runs.sqlite3").resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MCP_SKILLS_DIR", str(skills_root))
    monkeypatch.setenv("MCP_WORKSPACE_DIR", str(workspace_root))
    monkeypatch.setenv("MCP_RUN_STORE_SQLITE_PATH", str(db_path))
    module_path = Path("mcp-server/server.py").resolve()
    module_name = f"skill_runtime_service_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load skill runtime service module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_skill_request_writes_minimal_invocation_payload(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    workspace_dir = (tmp_path / "run-workspace").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    request_path = module._write_skill_request(
        workspace_dir=workspace_dir,
        skill_payload={
            "name": "pptx",
            "summary": "Create and edit PowerPoint decks.",
            "description": "Create and edit PowerPoint decks.",
            "tags": ["slides", "presentation"],
            "path": str((tmp_path / "skills" / "pptx").resolve()),
            "skill_md": "# PPTX Skill\n\nUse markitdown, Pillow, and pptxgenjs when needed.\n",
            "shell_hints": {
                "runnable_scripts": ["scripts/create.py"],
            },
        },
        goal="Create deck",
        user_query="帮我做一个 PPT",
        constraints={"output_format": "pptx"},
        workspace="finance",
        runtime_target={"platform": "linux", "shell": "/bin/sh"},
        command_plan={
            "mode": "generated_script",
            "shell_mode": "free_shell",
            "hints": {
                "auto_inferred_constraints": {
                    "topic": "生成式人工智能起源发展",
                    "slide_count": "10",
                }
            },
        },
    )

    payload = json.loads(request_path.read_text(encoding="utf-8"))

    assert request_path.name == "skill_invocation.json"
    assert payload["kind"] == "skill_invocation"
    assert payload["workspace"] == "finance"
    assert payload["runtime_target"]["platform"] == "linux"
    assert payload["constraints"] == {"output_format": "pptx"}
    assert payload["effective_constraints"]["output_format"] == "pptx"
    assert payload["effective_constraints"]["topic"] == "生成式人工智能起源发展"
    assert payload["planning_mode"] == "generated_script"
    assert payload["shell_mode"] == "free_shell"
    assert "skill" not in payload
    assert "shell_hints" not in payload


def test_write_skill_context_writes_full_skill_markdown_only(
    tmp_path: Path,
    monkeypatch,
):
    module = _load_runtime_service_module(tmp_path=tmp_path, monkeypatch=monkeypatch)
    workspace_dir = (tmp_path / "run-workspace").resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    context_path = module._write_skill_context(
        workspace_dir=workspace_dir,
        skill_payload={
            "name": "pptx",
            "description": "Create and edit PowerPoint decks.",
            "tags": ["slides", "presentation"],
            "path": str((tmp_path / "skills" / "pptx").resolve()),
            "skill_md": "# PPTX Skill\n\nUse markitdown, Pillow, and pptxgenjs when needed.\n",
        },
    )

    payload = json.loads(context_path.read_text(encoding="utf-8"))

    assert context_path.name == "skill_context.json"
    assert payload["kind"] == "skill_context"
    assert payload["skill"]["name"] == "pptx"
    assert payload["skill"]["description"] == "Create and edit PowerPoint decks."
    assert payload["skill_md"].startswith("# PPTX Skill")
    assert "markitdown" in payload["skill_md"]
    assert "references" not in payload
    assert "file_inventory" not in payload
    assert "shell_hints" not in payload
