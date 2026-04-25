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
    OUTPUT_ROOT,
)
from .utils import truncate_log, truncate_utf8_text

INTERNAL_RUNTIME_DIRNAME = ".skill_runtime"
TERMINAL_SNAPSHOT_FILENAME = "terminal_run_record.json"
SKILL_INVOCATION_FILENAME = "skill_invocation.json"
SKILL_CONTEXT_FILENAME = "skill_context.json"
TRANSPORT_WARNING_LIMIT = 3
TRANSPORT_WARNING_CHARS = 240
TRANSPORT_HISTORY_LIMIT = 3
TRANSPORT_HISTORY_TAIL_CHARS = 400


def _skill_invocation_path(workspace_dir: Path) -> Path:
    return (workspace_dir / SKILL_INVOCATION_FILENAME).resolve()


def _skill_context_path(workspace_dir: Path) -> Path:
    return (workspace_dir / SKILL_CONTEXT_FILENAME).resolve()


def _build_effective_constraints(
    *,
    constraints: dict[str, Any],
    command_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = dict(constraints or {})
    hints = (
        dict(command_plan.get("hints", {}))
        if isinstance(command_plan, dict) and isinstance(command_plan.get("hints"), dict)
        else {}
    )
    inferred = hints.get("auto_inferred_constraints")
    if isinstance(inferred, dict):
        for key, value in inferred.items():
            normalized_key = str(key).strip()
            if normalized_key and normalized_key not in effective:
                effective[normalized_key] = value
    return effective


def _write_skill_context(
    *,
    workspace_dir: Path,
    skill_payload: dict[str, Any],
) -> Path:
    context_path = _skill_context_path(workspace_dir)
    context_path.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "skill_context",
                "skill": {
                    "name": skill_payload["name"],
                    "description": str(skill_payload["description"]).strip(),
                    "tags": skill_payload["tags"],
                    "path": skill_payload["path"],
                },
                "skill_md": str(skill_payload.get("skill_md", "")),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return context_path


def _write_skill_request(
    *,
    workspace_dir: Path,
    skill_payload: dict[str, Any],
    goal: str,
    user_query: str,
    constraints: dict[str, Any],
    workspace: str | None = None,
    runtime_target: dict[str, Any] | None = None,
    command_plan: dict[str, Any] | None = None,
) -> Path:
    request_path = _skill_invocation_path(workspace_dir)
    request_path.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "skill_invocation",
                "skill_name": skill_payload["name"],
                "goal": goal,
                "user_query": user_query,
                "workspace": workspace,
                "runtime_target": dict(runtime_target or {}),
                "constraints": dict(constraints or {}),
                "effective_constraints": _build_effective_constraints(
                    constraints=constraints,
                    command_plan=command_plan,
                ),
                "planning_mode": (
                    str(command_plan.get("mode", "")).strip()
                    if isinstance(command_plan, dict)
                    else ""
                )
                or None,
                "shell_mode": (
                    str(command_plan.get("shell_mode", "")).strip()
                    if isinstance(command_plan, dict)
                    else ""
                )
                or None,
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
    parts = [part for part in normalized.split("/") if part]
    suffix = Path(normalized).suffix.lower()
    return (
        not normalized
        or any(part.startswith(".") for part in parts)
        or suffix in {".db", ".sqlite", ".sqlite3", ".sqlite-shm", ".sqlite-wal"}
        or normalized.startswith(f"{INTERNAL_RUNTIME_DIRNAME}/")
        or normalized
        in {
            SKILL_CONTEXT_FILENAME,
            SKILL_INVOCATION_FILENAME,
            "skill_runtime_runs.sqlite3",
            "skill_runtime_runs.sqlite3-shm",
            "skill_runtime_runs.sqlite3-wal",
        }
    )


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


def _sync_workspace_output_to_shared_output(*, workspace_dir: Path) -> None:
    if OUTPUT_ROOT is None:
        return
    source_output_dir = (workspace_dir / "output").resolve()
    if not source_output_dir.is_dir():
        return
    shared_output_dir = (OUTPUT_ROOT / workspace_dir.name).resolve()
    if shared_output_dir == source_output_dir:
        return
    if source_output_dir in shared_output_dir.parents:
        return
    shared_output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source_output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(source_output_dir)
        target = (shared_output_dir / relative_path).resolve()
        if target == shared_output_dir or shared_output_dir not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _collect_workspace_artifacts(workspace_dir: Path) -> list[dict[str, Any]]:
    artifacts_by_path: dict[str, dict[str, Any]] = {}
    mirrored_skill_paths = _load_mirrored_skill_paths(workspace_dir=workspace_dir)
    _sync_workspace_output_to_shared_output(workspace_dir=workspace_dir)
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
        artifacts_by_path[relative_path] = {
            "path": relative_path,
            "size_bytes": path.stat().st_size,
        }
    if OUTPUT_ROOT is not None:
        shared_output_dir = (OUTPUT_ROOT / workspace_dir.name).resolve()
        if shared_output_dir.is_dir():
            for path in sorted(shared_output_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative_path = str(path.relative_to(shared_output_dir)).replace("\\", "/")
                public_path = (
                    f"output/{relative_path}"
                    if relative_path
                    else "output"
                )
                artifacts_by_path[public_path] = {
                    "path": public_path,
                    "size_bytes": path.stat().st_size,
                }
    return [artifacts_by_path[key] for key in sorted(artifacts_by_path)]


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
    compacted: dict[str, Any] = {}
    for key in (
        "constraints",
        "command",
        "mode",
        "shell_mode",
        "rationale",
        "entrypoint",
        "cli_args",
        "generated_files",
        "bootstrap_commands",
        "bootstrap_reason",
        "missing_fields",
        "failure_reason",
        "hints",
    ):
        if key in command_plan:
            compacted[key] = command_plan[key]
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
    hints = compacted.get("hints")
    if isinstance(hints, dict):
        compacted["hints"] = _compact_command_plan_hints_for_transport(hints)
    return compacted


def _truncate_transport_text(value: Any, *, max_chars: int) -> str:
    preview, _ = truncate_utf8_text(str(value or ""), max_bytes=max_chars * 4)
    return preview


def _compact_warning_list(warnings: Any) -> list[str]:
    compacted: list[str] = []
    if not isinstance(warnings, list):
        return compacted
    for item in warnings[:TRANSPORT_WARNING_LIMIT]:
        compacted.append(
            _truncate_transport_text(item, max_chars=TRANSPORT_WARNING_CHARS)
        )
    return compacted


def _compact_constraint_inference_for_transport(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    compacted: dict[str, Any] = {}
    for key in ("planner", "status", "confidence"):
        if key in value:
            compacted[key] = value[key]
    applied = value.get("applied")
    if isinstance(applied, dict) and applied:
        compacted["applied"] = dict(applied)
    return compacted or None


def _compact_planner_attempts_for_transport(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    compacted: list[dict[str, Any]] = []
    for item in value[-6:]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "attempt_index": item.get("attempt_index"),
                "label": item.get("label"),
                "transport": item.get("transport"),
                "max_tokens": item.get("max_tokens"),
                "success": item.get("success"),
                "error_summary": _truncate_transport_text(
                    item.get("error_summary", ""),
                    max_chars=TRANSPORT_WARNING_CHARS,
                ),
            }
        )
    return compacted or None


def _compact_blockers_for_transport(value: Any) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return compacted
    for item in value[:TRANSPORT_HISTORY_LIMIT]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "failure_reason": item.get("failure_reason"),
                "missing_fields": list(item.get("missing_fields", []))
                if isinstance(item.get("missing_fields"), list)
                else [],
                "rationale": _truncate_transport_text(
                    item.get("rationale", ""),
                    max_chars=TRANSPORT_WARNING_CHARS,
                )
                or None,
            }
        )
    return compacted


def _compact_command_plan_hints_for_transport(hints: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in (
        "planner",
        "planner_context_mode",
        "planner_transport",
        "shell_mode_requested",
        "shell_mode_effective",
        "shell_mode_escalated",
        "shell_mode_escalation_reason",
        "auto_inferred_constraints",
        "manual_required_kind",
        "planner_error_summary",
        "generated_files_transport_compacted",
        "promoted_inline_python_to_generated_script",
    ):
        if key not in hints:
            continue
        value = hints[key]
        if key == "planner_error_summary":
            compacted[key] = _truncate_transport_text(value, max_chars=TRANSPORT_WARNING_CHARS)
        elif key == "auto_inferred_constraints" and isinstance(value, dict):
            compacted[key] = dict(value)
        else:
            compacted[key] = value
    required_tools = hints.get("required_tools")
    if isinstance(required_tools, list) and required_tools:
        compacted["required_tools"] = [
            str(item)
            for item in required_tools[:8]
            if isinstance(item, (str, int, float))
        ]
    warnings = _compact_warning_list(hints.get("warnings"))
    if warnings:
        compacted["warnings"] = warnings
    blockers = _compact_blockers_for_transport(hints.get("planning_blockers"))
    if blockers:
        compacted["planning_blockers"] = blockers
    constraint_inference = _compact_constraint_inference_for_transport(
        hints.get("constraint_inference")
    )
    if constraint_inference:
        compacted["constraint_inference"] = constraint_inference
    planner_attempts = _compact_planner_attempts_for_transport(hints.get("planner_attempts"))
    if planner_attempts:
        compacted["planner_attempts"] = planner_attempts
    return compacted


def _compact_skill_for_transport(skill: Any) -> dict[str, Any] | None:
    if not isinstance(skill, dict):
        return None
    compacted: dict[str, Any] = {}
    for key in ("name", "path", "tags"):
        if key in skill:
            compacted[key] = skill[key]
    return compacted or None


def _compact_history_entries(history: Any) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    if not isinstance(history, list):
        return compacted
    for item in history[-TRANSPORT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "attempt_index": item.get("attempt_index"),
                "snapshot_run_id": item.get("snapshot_run_id"),
                "stage": item.get("stage"),
                "command": _truncate_transport_text(
                    item.get("command", ""),
                    max_chars=TRANSPORT_WARNING_CHARS,
                )
                or None,
                "success": item.get("success"),
                "failure_reason": item.get("failure_reason"),
                "exit_code": item.get("exit_code"),
                "duration_s": item.get("duration_s"),
                "stdout_tail": _truncate_transport_text(
                    item.get("stdout_tail", ""),
                    max_chars=TRANSPORT_HISTORY_TAIL_CHARS,
                ),
                "stderr_tail": _truncate_transport_text(
                    item.get("stderr_tail", ""),
                    max_chars=TRANSPORT_HISTORY_TAIL_CHARS,
                ),
            }
        )
    return compacted


def _promote_transport_fields_from_command_plan(
    payload: dict[str, Any],
    command_plan: dict[str, Any],
) -> None:
    hints = (
        dict(command_plan.get("hints", {}))
        if isinstance(command_plan.get("hints"), dict)
        else {}
    )
    for top_level_key, hint_key in (
        ("planner_context_mode", "planner_context_mode"),
        ("planner_transport", "planner_transport"),
        ("planner_attempts", "planner_attempts"),
        ("shell_mode_requested", "shell_mode_requested"),
        ("shell_mode_effective", "shell_mode_effective"),
        ("shell_mode_escalated", "shell_mode_escalated"),
        ("shell_mode_escalation_reason", "shell_mode_escalation_reason"),
        ("auto_inferred_constraints", "auto_inferred_constraints"),
        ("planning_blockers", "planning_blockers"),
        ("manual_required_kind", "manual_required_kind"),
        ("planner_error_summary", "planner_error_summary"),
    ):
        if top_level_key not in payload and hint_key in hints:
            payload[top_level_key] = hints.get(hint_key)


def _prepare_transport_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transport_payload = dict(payload)
    command_plan = transport_payload.get("command_plan")
    if isinstance(command_plan, dict):
        transport_payload["command_plan"] = _compact_command_plan_for_transport(command_plan)
        _promote_transport_fields_from_command_plan(
            transport_payload,
            transport_payload["command_plan"],
        )
    skill = _compact_skill_for_transport(transport_payload.get("skill"))
    if skill is not None:
        transport_payload["skill"] = skill
    for noisy_key in ("file_inventory", "references", "shell_hints"):
        transport_payload.pop(noisy_key, None)
    if "repair_history" in transport_payload:
        transport_payload["repair_history"] = _compact_history_entries(
            transport_payload.get("repair_history")
        )
    if "bootstrap_history" in transport_payload:
        transport_payload["bootstrap_history"] = _compact_history_entries(
            transport_payload.get("bootstrap_history")
        )
    return transport_payload


def _truncate_log(text: str) -> tuple[str, bool]:
    return truncate_log(text, max_bytes=MAX_LOG_PREVIEW_BYTES)
