from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from kg_agent.skills.models import LoadedSkill

from .config import (
    MAX_LOG_PREVIEW_BYTES,
    MAX_TRANSPORT_GENERATED_FILE_PREVIEW_BYTES,
    MAX_TRANSPORT_GENERATED_FILES_TOTAL_BYTES,
)
from .utils import truncate_log, truncate_utf8_text

INTERNAL_RUNTIME_DIRNAME = ".skill_runtime"
TERMINAL_SNAPSHOT_FILENAME = "terminal_run_record.json"


def _write_skill_request(
    *,
    workspace_dir: Path,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
) -> Path:
    request_path = workspace_dir / "skill_request.json"
    request_path.write_text(
        json.dumps(
            {
                "skill_name": skill_payload["name"],
                "goal": goal,
                "user_query": user_query,
                "constraints": constraints,
                "skill": {
                    "name": skill_payload["name"],
                    "description": skill_payload["description"],
                    "tags": skill_payload["tags"],
                    "path": skill_payload["path"],
                },
                "shell_hints": skill_payload["shell_hints"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return request_path


def _materialize_skill_workspace_view(
    *,
    loaded_skill: LoadedSkill,
    workspace_dir: Path,
) -> list[str]:
    mirrored_paths: list[str] = []
    skill_root = loaded_skill.skill.path.resolve()
    for item in loaded_skill.file_inventory:
        relative_path = item.path.replace("\\", "/").strip()
        if not relative_path:
            continue
        source = (skill_root / relative_path).resolve()
        if not source.is_file():
            continue
        target = (workspace_dir / relative_path).resolve()
        if target == workspace_dir or workspace_dir not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        mirrored_paths.append(str(target.relative_to(workspace_dir)).replace("\\", "/"))
    return mirrored_paths


def _internal_runtime_dir(workspace_dir: Path) -> Path:
    return (workspace_dir / INTERNAL_RUNTIME_DIRNAME).resolve()


def _terminal_snapshot_path(workspace_dir: Path) -> Path:
    return (_internal_runtime_dir(workspace_dir) / TERMINAL_SNAPSHOT_FILENAME).resolve()


def _mirrored_skill_manifest_path(workspace_dir: Path) -> Path:
    return (_internal_runtime_dir(workspace_dir) / "mirrored_skill_files.json").resolve()


def _is_internal_workspace_artifact(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip()
    return normalized.startswith(f"{INTERNAL_RUNTIME_DIRNAME}/")


def _is_hidden_bootstrap_artifact(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").strip()
    return normalized.startswith(".skill_bootstrap/")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def _write_terminal_snapshot(*, workspace_dir: Path, record: dict[str, Any]) -> None:
    _write_json_atomic(
        _terminal_snapshot_path(workspace_dir),
        {
            "version": 1,
            "run_id": str(record.get("run_id", "")).strip(),
            "record": dict(record),
        },
    )


def _write_mirrored_skill_manifest(*, workspace_dir: Path, mirrored_paths: list[str]) -> None:
    _write_json_atomic(
        _mirrored_skill_manifest_path(workspace_dir),
        {
            "paths": [
                str(item).replace("\\", "/").strip()
                for item in mirrored_paths
                if str(item).strip()
            ]
        },
    )


def _load_mirrored_skill_paths(*, workspace_dir: Path) -> set[str]:
    manifest_path = _mirrored_skill_manifest_path(workspace_dir)
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    raw_paths = payload.get("paths") if isinstance(payload, dict) else None
    if not isinstance(raw_paths, list):
        return set()
    return {
        str(item).replace("\\", "/").strip()
        for item in raw_paths
        if str(item).strip()
    }


def _load_terminal_snapshot(
    *,
    workspace_dir: Path,
    run_id: str,
) -> dict[str, Any] | None:
    snapshot_path = _terminal_snapshot_path(workspace_dir)
    if not snapshot_path.is_file():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    record = payload.get("record")
    if not isinstance(record, dict):
        return None
    if str(record.get("run_id", "")).strip() != run_id:
        return None
    run_status = str(record.get("run_status", "")).strip()
    if run_status not in {"planned", "completed", "failed", "manual_required"}:
        return None
    return dict(record)


def _collect_workspace_artifacts(workspace_dir: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    mirrored_skill_paths = _load_mirrored_skill_paths(workspace_dir=workspace_dir)
    for path in sorted(workspace_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = str(path.relative_to(workspace_dir)).replace("\\", "/")
        if _is_internal_workspace_artifact(relative_path):
            continue
        if _is_hidden_bootstrap_artifact(relative_path):
            continue
        if relative_path in mirrored_skill_paths:
            continue
        artifacts.append(
            {
                "path": relative_path,
                "size_bytes": path.stat().st_size,
            }
        )
    return artifacts


def _compact_generated_files_for_transport(
    generated_files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    remaining_budget = max(0, MAX_TRANSPORT_GENERATED_FILES_TOTAL_BYTES)
    compacted: list[dict[str, Any]] = []
    any_truncated = False
    for raw_entry in generated_files:
        entry = dict(raw_entry)
        content = str(entry.get("content")) if entry.get("content") is not None else ""
        content_bytes = len(content.encode("utf-8"))
        preview_budget = min(
            max(0, MAX_TRANSPORT_GENERATED_FILE_PREVIEW_BYTES),
            remaining_budget,
        )
        preview, truncated = truncate_utf8_text(content, preview_budget)
        preview_bytes = len(preview.encode("utf-8"))
        remaining_budget = max(0, remaining_budget - preview_bytes)
        entry["content"] = preview
        entry["content_bytes"] = content_bytes
        entry["content_truncated"] = truncated
        compacted.append(entry)
        any_truncated = any_truncated or truncated
    return compacted, any_truncated


def _compact_command_plan_for_transport(command_plan: dict[str, Any]) -> dict[str, Any]:
    compacted = dict(command_plan)
    generated_files = compacted.get("generated_files")
    if isinstance(generated_files, list):
        compacted_generated_files, any_truncated = _compact_generated_files_for_transport(
            [dict(item) for item in generated_files if isinstance(item, dict)]
        )
        compacted["generated_files"] = compacted_generated_files
        if any_truncated:
            hints = (
                dict(compacted.get("hints", {}))
                if isinstance(compacted.get("hints"), dict)
                else {}
            )
            hints["generated_files_transport_compacted"] = True
            compacted["hints"] = hints
    return compacted


def _prepare_transport_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transport_payload = dict(payload)
    command_plan = transport_payload.get("command_plan")
    if isinstance(command_plan, dict):
        transport_payload["command_plan"] = _compact_command_plan_for_transport(
            command_plan
        )
    return transport_payload


def _truncate_log(text: str) -> tuple[str, bool]:
    return truncate_log(text, max_bytes=MAX_LOG_PREVIEW_BYTES)
