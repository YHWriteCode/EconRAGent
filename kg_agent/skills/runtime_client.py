from __future__ import annotations

from typing import Any

from kg_agent.config import SkillRuntimeConfig
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
        return self._normalize_run_payload(payload)

    async def cancel_skill_run(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.cancel_tool_name,
            arguments={"run_id": run_id},
        )
        return self._normalize_run_payload(payload)

    async def get_run_logs(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.logs_tool_name,
            arguments={"run_id": run_id},
        )
        return self._normalize_run_payload(payload)

    async def get_run_artifacts(self, *, run_id: str) -> dict[str, Any]:
        payload = await self._invoke_structured_tool(
            remote_name=self.config.artifacts_tool_name,
            arguments={"run_id": run_id},
        )
        return self._normalize_run_payload(payload)

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
            if "runtime_target" not in normalized and isinstance(
                command_plan.get("runtime_target"),
                dict,
            ):
                normalized["runtime_target"] = dict(command_plan["runtime_target"])
        elif "planning_mode" not in normalized:
            normalized["planning_mode"] = ""
        return normalized
