from __future__ import annotations

import csv
import json
from pathlib import Path, PurePosixPath
from typing import Any

from kg_agent.config import MCPServerConfig, SkillRuntimeConfig
from kg_agent.mcp.adapter import MCPAdapter
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillRunRecord,
    legacy_status_for_run_status,
    normalize_run_status,
)


class MCPBasedSkillRuntimeClient:
    MAX_ARTIFACT_PREVIEW_COUNT = 3
    MAX_ARTIFACT_PREVIEW_BYTES = 131072
    MAX_ARTIFACT_TEXT_PREVIEW_CHARS = 1600
    MAX_ARTIFACT_TABLE_PREVIEW_ROWS = 3

    def __init__(
        self,
        *,
        adapter: MCPAdapter,
        config: SkillRuntimeConfig,
    ):
        self.adapter = adapter
        self.config = config

    async def run_skill_task(
        self,
        *,
        request: SkillExecutionRequest,
        loaded_skill: LoadedSkill,
        command_plan: SkillCommandPlan,
    ) -> SkillRunRecord:
        result = await self.adapter.invoke_remote_tool(
            server_name=self.config.server,
            remote_name=self.config.run_tool_name,
            arguments={
                "skill_name": request.skill_name,
                "skill_path": str(loaded_skill.skill.path),
                "goal": request.goal,
                "user_query": request.user_query,
                "workspace": request.workspace,
                "constraints": request.constraints,
                "command_plan": command_plan.to_dict(),
            },
        )
        if not result.success:
            raise RuntimeError(result.error or result.summary())

        data = result.data if isinstance(result.data, dict) else {}
        structured = (
            data.get("structured_content")
            if isinstance(data.get("structured_content"), dict)
            else {}
        )
        payload = self._normalize_run_payload(
            structured or data,
            fallback_summary=result.summary(),
        )
        payload["runtime"] = self._runtime_metadata(remote_name=self.config.run_tool_name)
        payload["runtime_result"] = structured or data
        payload = self._map_workspace_to_host(payload)
        payload = self._attach_host_artifact_previews(payload)
        return SkillRunRecord.from_dict(
            payload,
            default_skill_name=request.skill_name,
            default_runtime=self._runtime_metadata(remote_name=self.config.run_tool_name),
        )

    async def get_run_status(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.status_tool_name,
            arguments={"run_id": run_id},
        )
        return self._map_workspace_to_host(self._normalize_run_payload(payload))

    async def cancel_skill_run(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.cancel_tool_name,
            arguments={"run_id": run_id},
        )
        return self._map_workspace_to_host(self._normalize_run_payload(payload))

    async def get_run_logs(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.logs_tool_name,
            arguments={"run_id": run_id},
        )
        return self._map_workspace_to_host(self._normalize_run_payload(payload))

    async def get_run_artifacts(self, *, run_id: str) -> dict[str, Any]:
        if not self.config.artifacts_tool_name.strip():
            return await self._load_artifacts_from_host_workspace(
                run_id=run_id,
                fallback_reason="artifacts_tool_disabled",
            )
        try:
            payload = await self._invoke_structured_tool(
                remote_name=self.config.artifacts_tool_name,
                arguments={"run_id": run_id},
            )
        except RuntimeError as exc:
            message = str(exc)
            if not self._is_transport_overflow_error(message):
                raise
            return await self._load_artifacts_from_host_workspace(
                run_id=run_id,
                fallback_reason=message,
            )
        normalized = self._map_workspace_to_host(self._normalize_run_payload(payload))
        return self._attach_host_artifact_previews(normalized)

    async def _invoke_structured_tool(
        self,
        *,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.adapter.invoke_remote_tool(
            server_name=self.config.server,
            remote_name=remote_name,
            arguments=arguments,
        )
        if not result.success:
            message = result.error or result.summary()
            if "Unknown run_id" in message:
                raise LookupError(message)
            raise RuntimeError(message)

        data = result.data if isinstance(result.data, dict) else {}
        structured = (
            data.get("structured_content")
            if isinstance(data.get("structured_content"), dict)
            else {}
        )
        if structured:
            return structured
        return data

    def _runtime_metadata(self, *, remote_name: str) -> dict[str, Any]:
        return {
            "executor": "mcp",
            "server": self.config.server,
            "remote_name": remote_name,
            "status_tool_name": self.config.status_tool_name,
            "cancel_tool_name": self.config.cancel_tool_name,
            "logs_tool_name": self.config.logs_tool_name,
            "artifacts_tool_name": self.config.artifacts_tool_name,
        }

    @staticmethod
    def _normalize_run_payload(
        payload: dict[str, Any],
        *,
        fallback_summary: str | None = None,
    ) -> dict[str, Any]:
        normalized = dict(payload)
        run_status = normalize_run_status(
            normalized.get("run_status", normalized.get("status")),
            success=bool(normalized.get("success")),
        )
        normalized["run_status"] = run_status
        normalized["status"] = legacy_status_for_run_status(run_status)
        if "summary" not in normalized and fallback_summary:
            normalized["summary"] = fallback_summary
        if "command_plan" not in normalized and isinstance(normalized.get("shell_plan"), dict):
            normalized["command_plan"] = dict(normalized["shell_plan"])
        command_plan = normalized.get("command_plan")
        if isinstance(command_plan, dict):
            normalized["planning_mode"] = str(command_plan.get("mode", "")).strip()
            if "shell_mode" not in normalized:
                normalized["shell_mode"] = str(command_plan.get("shell_mode", "")).strip()
            hints = (
                dict(command_plan.get("hints", {}))
                if isinstance(command_plan.get("hints"), dict)
                else {}
            )
            if "shell_mode_requested" not in normalized:
                normalized["shell_mode_requested"] = hints.get(
                    "shell_mode_requested",
                    str(command_plan.get("shell_mode", "")).strip(),
                )
            if "shell_mode_effective" not in normalized:
                normalized["shell_mode_effective"] = hints.get(
                    "shell_mode_effective",
                    str(command_plan.get("shell_mode", "")).strip(),
                )
            if "shell_mode_escalated" not in normalized:
                normalized["shell_mode_escalated"] = bool(
                    hints.get(
                        "shell_mode_escalated",
                        normalized.get("shell_mode_requested")
                        != normalized.get("shell_mode_effective"),
                    )
                )
            if (
                "shell_mode_escalation_reason" not in normalized
                and "shell_mode_escalation_reason" in hints
            ):
                normalized["shell_mode_escalation_reason"] = hints.get(
                    "shell_mode_escalation_reason"
                )
            if "auto_inferred_constraints" not in normalized and isinstance(
                hints.get("auto_inferred_constraints"),
                dict,
            ):
                normalized["auto_inferred_constraints"] = dict(
                    hints["auto_inferred_constraints"]
                )
            if "planning_blockers" not in normalized and isinstance(
                hints.get("planning_blockers"),
                list,
            ):
                normalized["planning_blockers"] = [
                    dict(item)
                    for item in hints["planning_blockers"]
                    if isinstance(item, dict)
                ]
            if "runtime_target" not in normalized and isinstance(
                command_plan.get("runtime_target"),
                dict,
            ):
                normalized["runtime_target"] = dict(command_plan["runtime_target"])
        elif "planning_mode" not in normalized:
            normalized["planning_mode"] = ""
        return normalized

    async def _load_artifacts_from_host_workspace(
        self,
        *,
        run_id: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        status = await self.get_run_status(run_id=run_id)
        runtime = (
            dict(status.get("runtime", {}))
            if isinstance(status.get("runtime"), dict)
            else {}
        )
        runtime_workspace = (
            str(runtime.get("container_workspace", "")).strip()
            if isinstance(runtime.get("container_workspace"), str)
            and str(runtime.get("container_workspace"),).strip()
            else (
                str(status.get("workspace", "")).strip()
                if isinstance(status.get("workspace"), str)
                else ""
            )
        )
        runtime_target = (
            dict(status.get("runtime_target", {}))
            if isinstance(status.get("runtime_target"), dict)
            else {}
        )
        host_workspace = self._resolve_host_workspace_path(
            runtime_workspace=runtime_workspace,
            runtime_target=runtime_target,
        )
        if host_workspace is None or not host_workspace.is_dir():
            raise RuntimeError(fallback_reason)

        artifacts = self._collect_host_workspace_artifacts(host_workspace)
        runtime["artifacts_host_fallback"] = True
        runtime["artifacts_host_fallback_reason"] = fallback_reason
        runtime["container_workspace"] = runtime_workspace
        payload = self._normalize_run_payload(
            {
                "summary": f"Loaded artifacts for run '{run_id}' from the mounted host workspace.",
                "run_id": run_id,
                "skill_name": status.get("skill_name"),
                "run_status": status.get("run_status"),
                "status": status.get("status"),
                "success": bool(status.get("success")),
                "shell_mode": status.get("shell_mode"),
                "runtime_target": runtime_target,
                "workspace": str(host_workspace),
                "started_at": status.get("started_at"),
                "finished_at": status.get("finished_at"),
                "failure_reason": status.get("failure_reason"),
                "runtime": runtime,
                "preflight": dict(status.get("preflight", {}))
                if isinstance(status.get("preflight"), dict)
                else {},
                "repair_attempted": bool(status.get("repair_attempted", False)),
                "repair_succeeded": bool(status.get("repair_succeeded", False)),
                "repaired_from_run_id": status.get("repaired_from_run_id"),
                "repair_attempt_count": int(status.get("repair_attempt_count", 0) or 0),
                "repair_attempt_limit": int(status.get("repair_attempt_limit", 0) or 0),
                "repair_history": list(status.get("repair_history", []))
                if isinstance(status.get("repair_history"), list)
                else [],
                "bootstrap_attempted": bool(status.get("bootstrap_attempted", False)),
                "bootstrap_succeeded": bool(status.get("bootstrap_succeeded", False)),
                "bootstrap_attempt_count": int(
                    status.get("bootstrap_attempt_count", 0) or 0
                ),
                "bootstrap_attempt_limit": int(
                    status.get("bootstrap_attempt_limit", 0) or 0
                ),
                "bootstrap_history": list(status.get("bootstrap_history", []))
                if isinstance(status.get("bootstrap_history"), list)
                else [],
                "cancel_requested": bool(status.get("cancel_requested", False)),
                "artifacts": artifacts,
            }
        )
        return self._attach_host_artifact_previews(payload)

    def _map_workspace_to_host(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        runtime_workspace = (
            str(normalized.get("workspace", "")).strip()
            if isinstance(normalized.get("workspace"), str)
            else ""
        )
        if not runtime_workspace:
            return normalized
        runtime_target = (
            dict(normalized.get("runtime_target", {}))
            if isinstance(normalized.get("runtime_target"), dict)
            else {}
        )
        host_workspace = self._resolve_host_workspace_path(
            runtime_workspace=runtime_workspace,
            runtime_target=runtime_target,
        )
        if host_workspace is None:
            return normalized
        runtime = (
            dict(normalized.get("runtime", {}))
            if isinstance(normalized.get("runtime"), dict)
            else {}
        )
        runtime.setdefault("container_workspace", runtime_workspace)
        normalized["runtime"] = runtime
        normalized["workspace"] = str(host_workspace)
        return normalized

    def _resolve_host_workspace_path(
        self,
        *,
        runtime_workspace: str,
        runtime_target: dict[str, Any],
    ) -> Path | None:
        if not runtime_workspace:
            return None
        server_config = self.adapter.get_server_config(self.config.server)
        if server_config is None:
            return None
        resolved_mount = self._resolve_host_workspace_mount(
            server_config,
            runtime_workspace=runtime_workspace,
            runtime_target=runtime_target,
        )
        if resolved_mount is None:
            return None
        runtime_workspace_path = PurePosixPath(runtime_workspace)
        host_workspace_root, container_workspace_root = resolved_mount
        workspace_root_path = PurePosixPath(container_workspace_root)
        try:
            relative_path = runtime_workspace_path.relative_to(workspace_root_path)
        except ValueError:
            relative_path = PurePosixPath(runtime_workspace_path.name)
        return (host_workspace_root / Path(*relative_path.parts)).resolve()

    def _resolve_host_shared_output_path(
        self,
        *,
        runtime_workspace: str,
        runtime_target: dict[str, Any],
    ) -> Path | None:
        if not runtime_workspace:
            return None
        run_workspace_path = PurePosixPath(runtime_workspace)
        run_name = run_workspace_path.name
        if not run_name:
            return None
        workspace_root = str(runtime_target.get("workspace_root", "")).strip() or "/workspace"
        container_output_path = str(PurePosixPath(workspace_root) / "output" / run_name)
        return self._resolve_host_workspace_path(
            runtime_workspace=container_output_path,
            runtime_target=runtime_target,
        )

    @staticmethod
    def _resolve_host_workspace_mount(
        server_config: MCPServerConfig,
        *,
        runtime_workspace: str,
        runtime_target: dict[str, Any],
    ) -> tuple[Path, str] | None:
        command_name = Path(server_config.command).name.lower()
        if command_name not in {"docker", "docker.exe"}:
            return None
        workspace_root = str(runtime_target.get("workspace_root", "")).strip() or "/workspace"
        runtime_workspace_path = PurePosixPath(runtime_workspace)
        mounts: list[tuple[Path, str]] = []
        args = list(server_config.args)
        for index, arg in enumerate(args):
            if arg in {"-v", "--volume"} and index + 1 < len(args):
                resolved = MCPBasedSkillRuntimeClient._parse_docker_bind_mount(
                    args[index + 1],
                )
                if resolved is not None:
                    mounts.append(resolved)
            if arg == "--mount" and index + 1 < len(args):
                resolved = MCPBasedSkillRuntimeClient._parse_docker_mount_flag(
                    args[index + 1],
                )
                if resolved is not None:
                    mounts.append(resolved)
        if not mounts:
            return None

        best_match: tuple[Path, str] | None = None
        best_depth = -1
        for host_path, container_path in mounts:
            container_root = PurePosixPath(container_path)
            if runtime_workspace_path != container_root and container_root not in runtime_workspace_path.parents:
                continue
            depth = len(container_root.parts)
            if depth > best_depth:
                best_match = (host_path, container_path)
                best_depth = depth
        if best_match is not None:
            return best_match

        for host_path, container_path in mounts:
            if str(container_path).strip() == workspace_root:
                return host_path, container_path
        return None

    @staticmethod
    def _parse_docker_bind_mount(
        raw_value: str,
    ) -> tuple[Path, str] | None:
        value = str(raw_value or "").strip()
        if not value:
            return None
        mode = None
        host_part = None
        parts = value.split(":")
        if len(parts) < 2:
            return None
        if parts[-1] and not parts[-1].startswith("/"):
            mode = parts[-1]
            parts = parts[:-1]
        if len(parts) < 2:
            return None
        container_path = parts[-1]
        host_part = ":".join(parts[:-1])
        if mode and not mode.strip():
            mode = None
        host_path = Path(host_part)
        if not host_path.is_absolute():
            return None
        return host_path.resolve(), container_path.strip()

    @staticmethod
    def _parse_docker_mount_flag(
        raw_value: str,
    ) -> tuple[Path, str] | None:
        items: dict[str, str] = {}
        for chunk in str(raw_value or "").split(","):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            items[key.strip().lower()] = value.strip()
        mount_type = items.get("type", "").lower()
        target = items.get("target") or items.get("dst") or items.get("destination")
        source = items.get("source") or items.get("src")
        if mount_type and mount_type != "bind":
            return None
        if not source:
            return None
        source_path = Path(source)
        if not source_path.is_absolute():
            return None
        return source_path.resolve(), str(target or "").strip()

    @staticmethod
    def _collect_host_workspace_artifacts(workspace_dir: Path) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for path in sorted(workspace_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_path = str(path.relative_to(workspace_dir)).replace("\\", "/")
            if relative_path.startswith(".skill_runtime/"):
                continue
            if relative_path.startswith(".skill_bootstrap/"):
                continue
            artifacts.append(
                {
                    "path": relative_path,
                    "size_bytes": path.stat().st_size,
                }
            )
        return artifacts

    def _attach_host_artifact_previews(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        artifacts = normalized.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return normalized
        runtime = (
            dict(normalized.get("runtime", {}))
            if isinstance(normalized.get("runtime"), dict)
            else {}
        )
        runtime_workspace = (
            str(runtime.get("container_workspace", "")).strip()
            if isinstance(runtime.get("container_workspace"), str)
            and str(runtime.get("container_workspace", "")).strip()
            else str(normalized.get("workspace", "")).strip()
        )
        runtime_target = (
            dict(normalized.get("runtime_target", {}))
            if isinstance(normalized.get("runtime_target"), dict)
            else {}
        )
        host_workspace = self._resolve_host_workspace_path(
            runtime_workspace=runtime_workspace,
            runtime_target=runtime_target,
        )
        host_shared_output = self._resolve_host_shared_output_path(
            runtime_workspace=runtime_workspace,
            runtime_target=runtime_target,
        )
        previews: list[dict[str, Any]] = []
        for artifact in self._prioritize_preview_candidates(artifacts):
            artifact_path = str(artifact.get("path", "")).replace("\\", "/").strip()
            if not artifact_path:
                continue
            source_path = self._resolve_host_artifact_path(
                artifact_path=artifact_path,
                host_workspace=host_workspace,
                host_shared_output=host_shared_output,
            )
            if source_path is None or not source_path.is_file():
                continue
            preview = self._build_artifact_preview(
                artifact_path=artifact_path,
                source_path=source_path,
                size_bytes=int(artifact.get("size_bytes", 0) or 0),
            )
            if preview is not None:
                previews.append(preview)
            if len(previews) >= self.MAX_ARTIFACT_PREVIEW_COUNT:
                break
        if previews:
            normalized["artifact_previews"] = previews
        return normalized

    def _prioritize_preview_candidates(
        self,
        artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates = [item for item in artifacts if isinstance(item, dict)]
        return sorted(
            candidates,
            key=lambda item: (
                0
                if str(item.get("path", "")).replace("\\", "/").startswith("output/")
                else 1,
                0
                if Path(str(item.get("path", ""))).suffix.lower() in {".csv", ".json", ".txt", ".md", ".log"}
                else 1,
                str(item.get("path", "")),
            ),
        )

    @staticmethod
    def _resolve_host_artifact_path(
        *,
        artifact_path: str,
        host_workspace: Path | None,
        host_shared_output: Path | None,
    ) -> Path | None:
        normalized = artifact_path.replace("\\", "/").strip()
        if normalized.startswith("output/"):
            relative_output = normalized[len("output/") :]
            if host_shared_output is not None:
                candidate = (host_shared_output / Path(*PurePosixPath(relative_output).parts)).resolve()
                if candidate.is_file():
                    return candidate
            if host_workspace is not None:
                candidate = (host_workspace / Path(*PurePosixPath(normalized).parts)).resolve()
                if candidate.is_file():
                    return candidate
            return None
        if host_workspace is None:
            return None
        candidate = (host_workspace / Path(*PurePosixPath(normalized).parts)).resolve()
        return candidate if candidate.is_file() else None

    def _build_artifact_preview(
        self,
        *,
        artifact_path: str,
        source_path: Path,
        size_bytes: int,
    ) -> dict[str, Any] | None:
        suffix = source_path.suffix.lower()
        if size_bytes > self.MAX_ARTIFACT_PREVIEW_BYTES:
            return None
        if suffix == ".csv":
            return self._build_csv_artifact_preview(artifact_path=artifact_path, source_path=source_path)
        if suffix == ".json":
            return self._build_json_artifact_preview(artifact_path=artifact_path, source_path=source_path)
        if suffix in {".txt", ".md", ".log", ".yaml", ".yml"}:
            return self._build_text_artifact_preview(artifact_path=artifact_path, source_path=source_path)
        return None

    def _build_csv_artifact_preview(
        self,
        *,
        artifact_path: str,
        source_path: Path,
    ) -> dict[str, Any] | None:
        try:
            with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    return None
                rows = [dict(row) for row in reader]
        except Exception:
            return None
        head_rows = rows[: self.MAX_ARTIFACT_TABLE_PREVIEW_ROWS]
        tail_rows = rows[-self.MAX_ARTIFACT_TABLE_PREVIEW_ROWS :] if rows else []
        return {
            "path": artifact_path,
            "kind": "csv",
            "columns": list(reader.fieldnames),
            "row_count": len(rows),
            "head_rows": head_rows,
            "tail_rows": tail_rows,
        }

    def _build_json_artifact_preview(
        self,
        *,
        artifact_path: str,
        source_path: Path,
    ) -> dict[str, Any] | None:
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except Exception:
            return self._build_text_artifact_preview(
                artifact_path=artifact_path,
                source_path=source_path,
            )
        preview: dict[str, Any] = {
            "path": artifact_path,
            "kind": "json",
            "json_type": type(payload).__name__,
        }
        if isinstance(payload, dict):
            preview["keys"] = list(payload.keys())[:20]
            preview["value_preview"] = {
                key: payload[key]
                for key in list(payload.keys())[:10]
            }
        elif isinstance(payload, list):
            preview["row_count"] = len(payload)
            preview["items_preview"] = payload[: self.MAX_ARTIFACT_TABLE_PREVIEW_ROWS]
        else:
            preview["value"] = payload
        return preview

    def _build_text_artifact_preview(
        self,
        *,
        artifact_path: str,
        source_path: Path,
    ) -> dict[str, Any] | None:
        try:
            text = source_path.read_text(encoding="utf-8")
        except Exception:
            return None
        excerpt = text[: self.MAX_ARTIFACT_TEXT_PREVIEW_CHARS]
        return {
            "path": artifact_path,
            "kind": "text",
            "excerpt": excerpt,
            "truncated": len(text) > len(excerpt),
        }

    @staticmethod
    def _is_transport_overflow_error(message: str) -> bool:
        normalized = str(message or "")
        return (
            "chunk exceed the limit" in normalized
            or "chunk is longer than limit" in normalized
        )
