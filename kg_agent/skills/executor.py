from __future__ import annotations

from typing import Any, Protocol

from kg_agent.skills.loader import SkillLoader
from kg_agent.skills.models import LoadedSkill, SkillExecutionRequest
from kg_agent.skills.registry import SkillRegistry
from kg_agent.tools.base import ToolResult


class SkillRuntimeClient(Protocol):
    async def run_skill_task(
        self,
        *,
        request: SkillExecutionRequest,
        loaded_skill: LoadedSkill,
    ) -> dict[str, Any]:
        ...


class SkillExecutor:
    def __init__(
        self,
        *,
        registry: SkillRegistry,
        loader: SkillLoader | None = None,
        runtime_client: SkillRuntimeClient | None = None,
    ):
        self.registry = registry
        self.loader = loader or SkillLoader(registry)
        self.runtime_client = runtime_client

    async def execute(
        self,
        *,
        skill_name: str,
        goal: str,
        user_query: str,
        workspace: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> ToolResult:
        skill = self.registry.get(skill_name)
        tool_name = f"skill:{skill_name}"
        if skill is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Skill is not registered: {skill_name}",
                metadata={"executor": "skill", "skill_name": skill_name},
            )

        loaded_skill = self.loader.load_skill(skill_name)
        request = SkillExecutionRequest(
            skill_name=skill_name,
            goal=goal,
            user_query=user_query,
            workspace=workspace,
            constraints=constraints or {},
        )
        data: dict[str, Any] = {
            "status": "prepared",
            "skill_name": skill_name,
            "goal": goal,
            "user_query": user_query,
            "workspace": workspace,
            "constraints": dict(constraints or {}),
            "skill": skill.to_catalog_dict(),
            "file_inventory": [item.to_dict() for item in loaded_skill.file_inventory],
            "summary": (
                f"Prepared skill '{skill_name}' for runtime execution."
                if self.runtime_client is not None
                else f"Loaded skill '{skill_name}' and prepared its local runtime context."
            ),
        }

        if self.runtime_client is not None:
            try:
                runtime_data = await self.runtime_client.run_skill_task(
                    request=request,
                    loaded_skill=loaded_skill,
                )
            except Exception as exc:
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=str(exc),
                    metadata={"executor": "skill", "skill_name": skill_name},
                )
            if isinstance(runtime_data, dict):
                data.update(runtime_data)

        return ToolResult(
            tool_name=tool_name,
            success=bool(data.get("success", True)),
            data=data,
            metadata={"executor": "skill", "skill_name": skill_name},
        )
