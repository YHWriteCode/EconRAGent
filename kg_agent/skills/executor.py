from __future__ import annotations

from typing import Any, Protocol
from pathlib import Path

from kg_agent.skills.command_planner import (
    SkillCommandPlanner,
    build_skill_doc_bundle,
    compact_skill_doc_bundle,
    extract_documented_entrypoint_paths,
)
from kg_agent.skills.loader import SkillLoader
from kg_agent.skills.models import (
    LoadedSkill,
    SkillCommandPlan,
    SkillExecutionRequest,
    SkillRunRecord,
    SkillRuntimeTarget,
)
from kg_agent.skills.registry import SkillRegistry
from kg_agent.tools.base import ToolResult


class SkillRuntimeClient(Protocol):
    async def run_skill_task(
        self,
        *,
        request: SkillExecutionRequest,
        loaded_skill: LoadedSkill,
        command_plan: SkillCommandPlan,
    ) -> SkillRunRecord:
        ...

    async def get_run_status(self, *, run_id: str) -> dict[str, Any]:
        ...

    async def cancel_skill_run(self, *, run_id: str) -> dict[str, Any]:
        ...

    async def get_run_logs(self, *, run_id: str) -> dict[str, Any]:
        ...

    async def get_run_artifacts(self, *, run_id: str) -> dict[str, Any]:
        ...

    async def resolve_artifact_path(self, *, run_id: str, artifact_path: str) -> Path:
        ...


class SkillExecutor:
    def __init__(
        self,
        *,
        registry: SkillRegistry,
        loader: SkillLoader | None = None,
        command_planner: SkillCommandPlanner | None = None,
        runtime_client: SkillRuntimeClient | None = None,
    ):
        self.registry = registry
        self.loader = loader or SkillLoader(registry)
        self.command_planner = command_planner or SkillCommandPlanner()
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
            runtime_target=getattr(
                self.command_planner,
                "default_runtime_target",
                SkillRuntimeTarget.linux_default(),
            ),
            constraints=constraints or {},
        )
        if self._is_doc_advisory_skill(loaded_skill):
            return self._build_doc_advisory_result(
                skill_name=skill_name,
                goal=goal,
                user_query=user_query,
                workspace=workspace,
                constraints=constraints or {},
                loaded_skill=loaded_skill,
                request=request,
            )
        command_plan = await self.command_planner.plan(
            loaded_skill=loaded_skill,
            request=request,
        )

        run_record: SkillRunRecord
        if self.runtime_client is not None:
            try:
                run_record = await self.runtime_client.run_skill_task(
                    request=request,
                    loaded_skill=loaded_skill,
                    command_plan=command_plan,
                )
            except Exception as exc:
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=str(exc),
                    metadata={"executor": "skill", "skill_name": skill_name},
                )
        else:
            if command_plan.is_manual_required:
                run_record = SkillRunRecord(
                    skill_name=skill_name,
                    run_status="manual_required",
                    success=False,
                    summary=command_plan.rationale
                    or (
                        f"Skill '{skill_name}' could not be planned automatically."
                    ),
                    command_plan=command_plan,
                    command=command_plan.command,
                    workspace=workspace,
                    failure_reason=command_plan.failure_reason,
                    runtime={"executor": "skill", "mode": "local_preview"},
                )
            else:
                run_record = SkillRunRecord(
                    skill_name=skill_name,
                    run_status="planned",
                    success=True,
                    summary=(
                        f"Planned skill '{skill_name}' locally without executing a runtime."
                    ),
                    command_plan=command_plan,
                    command=command_plan.command,
                    workspace=workspace,
                    runtime={"executor": "skill", "mode": "local_preview"},
                )

        data: dict[str, Any] = run_record.to_public_dict()
        data.update(
            {
                "skill_name": skill_name,
                "goal": goal,
                "user_query": user_query,
                "workspace": run_record.workspace or workspace,
                "requested_workspace": workspace,
                "constraints": dict(constraints or {}),
                "skill": {
                    "name": skill.name,
                    "path": str(skill.path),
                    "tags": list(skill.tags),
                },
            }
        )

        return ToolResult(
            tool_name=tool_name,
            success=run_record.success,
            data=data,
            metadata={"executor": "skill", "skill_name": skill_name},
            error=None if run_record.success else run_record.summary,
        )

    @staticmethod
    def _is_doc_advisory_skill(loaded_skill: LoadedSkill) -> bool:
        runtime_requirements = loaded_skill.skill.metadata.get("runtime_requirements")
        if isinstance(runtime_requirements, str) and runtime_requirements.strip():
            return False
        if any(item.kind == "script" for item in loaded_skill.file_inventory):
            return False
        if extract_documented_entrypoint_paths(loaded_skill.skill_md):
            return False
        return True

    def _build_doc_advisory_result(
        self,
        *,
        skill_name: str,
        goal: str,
        user_query: str,
        workspace: str | None,
        constraints: dict[str, Any],
        loaded_skill: LoadedSkill,
        request: SkillExecutionRequest,
    ) -> ToolResult:
        tool_name = f"skill:{skill_name}"
        doc_bundle = compact_skill_doc_bundle(build_skill_doc_bundle(loaded_skill, request))
        data = {
            "summary": (
                f"Loaded advisory guidance from skill '{skill_name}' directly from "
                "SKILL.md and progressively discovered follow-up docs."
            ),
            "status": "completed",
            "run_status": "completed",
            "skill_name": skill_name,
            "goal": goal,
            "user_query": user_query,
            "workspace": workspace,
            "requested_workspace": workspace,
            "constraints": dict(constraints),
            "execution_mode": "doc_advisory",
            "advisory_mode": "doc_only",
            "command_plan": {
                "mode": "doc_advisory",
                "entrypoint": None,
                "missing_fields": [],
                "failure_reason": None,
            },
            "doc_bundle": doc_bundle,
            "skill": {
                "name": loaded_skill.skill.name,
                "description": loaded_skill.skill.description,
                "path": str(loaded_skill.skill.path),
                "tags": list(loaded_skill.skill.tags),
            },
        }
        return ToolResult(
            tool_name=tool_name,
            success=True,
            data=data,
            metadata={"executor": "skill", "skill_name": skill_name},
        )

    async def get_run_status(self, *, run_id: str) -> dict[str, Any]:
        if self.runtime_client is None:
            raise RuntimeError("Skill runtime client is not configured")
        return await self.runtime_client.get_run_status(run_id=run_id)

    async def cancel_skill_run(self, *, run_id: str) -> dict[str, Any]:
        if self.runtime_client is None:
            raise RuntimeError("Skill runtime client is not configured")
        return await self.runtime_client.cancel_skill_run(run_id=run_id)

    async def get_run_logs(self, *, run_id: str) -> dict[str, Any]:
        if self.runtime_client is None:
            raise RuntimeError("Skill runtime client is not configured")
        return await self.runtime_client.get_run_logs(run_id=run_id)

    async def get_run_artifacts(self, *, run_id: str) -> dict[str, Any]:
        if self.runtime_client is None:
            raise RuntimeError("Skill runtime client is not configured")
        return await self.runtime_client.get_run_artifacts(run_id=run_id)

    async def resolve_artifact_path(self, *, run_id: str, artifact_path: str) -> Path:
        if self.runtime_client is None:
            raise RuntimeError("Skill runtime client is not configured")
        resolver = getattr(self.runtime_client, "resolve_artifact_path", None)
        if not callable(resolver):
            raise RuntimeError("Skill runtime client does not expose artifact files")
        return await resolver(run_id=run_id, artifact_path=artifact_path)
