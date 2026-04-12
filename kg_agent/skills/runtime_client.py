from __future__ import annotations

from typing import Any

from kg_agent.config import SkillRuntimeConfig
from kg_agent.mcp.adapter import MCPAdapter
from kg_agent.skills.models import LoadedSkill, SkillExecutionRequest


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
    ) -> dict[str, Any]:
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
        runtime_success = bool(structured.get("success", result.success))
        summary = (
            structured.get("summary")
            if isinstance(structured.get("summary"), str)
            else result.summary()
        )
        return {
            "status": structured.get("status", "completed"),
            "success": runtime_success,
            "summary": summary,
            "execution_mode": structured.get("execution_mode", "shell"),
            "run_id": structured.get("run_id"),
            "command": structured.get("command"),
            "workspace": structured.get("workspace"),
            "artifacts": structured.get("artifacts", []),
            "logs_preview": structured.get("logs_preview"),
            "shell_plan": structured.get("shell_plan"),
            "shell_hints": structured.get("shell_hints"),
            "runtime": {
                "executor": "mcp",
                "server": self.config.server,
                "remote_name": self.config.run_tool_name,
                "logs_tool_name": self.config.logs_tool_name,
                "artifacts_tool_name": self.config.artifacts_tool_name,
            },
            "runtime_result": structured or data,
        }

    async def get_run_logs(self, *, run_id: str) -> dict[str, Any]:
        return await self._invoke_structured_tool(
            remote_name=self.config.logs_tool_name,
            arguments={"run_id": run_id},
        )

    async def get_run_artifacts(self, *, run_id: str) -> dict[str, Any]:
        return await self._invoke_structured_tool(
            remote_name=self.config.artifacts_tool_name,
            arguments={"run_id": run_id},
        )

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
