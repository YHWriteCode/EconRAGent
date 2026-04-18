from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .service import RuntimeService


@dataclass
class TransportBindings:
    list_skills: Callable[..., dict[str, Any]]
    read_skill: Callable[..., dict[str, Any]]
    read_skill_file: Callable[..., dict[str, Any]]
    read_skill_docs: Callable[..., dict[str, Any]]
    run_skill_task: Callable[..., Any]
    get_run_status: Callable[..., dict[str, Any]]
    cancel_skill_run: Callable[..., Any]
    get_run_logs: Callable[..., dict[str, Any]]
    get_run_artifacts: Callable[..., dict[str, Any]]
    execute_skill_script: Callable[..., Any]
    skill_catalog_resource: Callable[..., str]
    skill_resource: Callable[..., str]
    skill_docs_resource: Callable[..., str]
    skill_file_resource: Callable[..., str]
    skill_reference_resource: Callable[..., str]
    skill_run_logs_resource: Callable[..., str]
    skill_run_artifacts_resource: Callable[..., str]


def build_transport_bindings(
    *,
    mcp: Any,
    service: RuntimeService,
    default_run_timeout_s: int,
    default_script_timeout_s: int,
) -> TransportBindings:
    @mcp.tool()
    def list_skills(query: str | None = None) -> dict[str, Any]:
        """Return the lightweight local skill catalog."""
        return service.list_skills(query)

    @mcp.tool()
    def read_skill(skill_name: str) -> dict[str, Any]:
        """Load SKILL.md and the indexed file inventory for one skill."""
        return service.read_skill(skill_name)

    @mcp.tool()
    def read_skill_file(skill_name: str, relative_path: str) -> dict[str, Any]:
        """Read one file from a skill directory by relative path."""
        return service.read_skill_file(skill_name, relative_path)

    @mcp.tool()
    def read_skill_docs(skill_name: str) -> dict[str, Any]:
        """Compatibility wrapper around the new coarse-grained read_skill interface."""
        return service.read_skill_docs(skill_name)

    @mcp.tool()
    async def run_skill_task(
        skill_name: str,
        goal: str,
        user_query: str | None = None,
        workspace: str | None = None,
        constraints: dict[str, Any] | None = None,
        command_plan: dict[str, Any] | None = None,
        timeout_s: int = default_run_timeout_s,
        cleanup_workspace: bool = False,
        wait_for_completion: bool | None = None,
    ) -> dict[str, Any]:
        """Execute a skill task through a shell-oriented runtime boundary."""
        return await service.run_skill_task(
            skill_name=skill_name,
            goal=goal,
            user_query=user_query,
            workspace=workspace,
            constraints=constraints,
            command_plan=command_plan,
            timeout_s=timeout_s,
            cleanup_workspace=cleanup_workspace,
            wait_for_completion=wait_for_completion,
        )

    @mcp.tool()
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Fetch lifecycle status for a prior shell-based skill run."""
        return service.get_run_status(run_id)

    @mcp.tool()
    async def cancel_skill_run(
        run_id: str,
        wait_for_termination: bool = True,
    ) -> dict[str, Any]:
        """Request cancellation for a background shell skill run."""
        return await service.cancel_skill_run(
            run_id,
            wait_for_termination=wait_for_termination,
        )

    @mcp.tool()
    def get_run_logs(run_id: str) -> dict[str, Any]:
        """Fetch full stdout/stderr logs for a prior shell-based skill run."""
        return service.get_run_logs(run_id)

    @mcp.tool()
    def get_run_artifacts(run_id: str) -> dict[str, Any]:
        """List artifacts produced by a prior shell-based skill run."""
        return service.get_run_artifacts(run_id)

    @mcp.tool()
    async def execute_skill_script(
        skill_name: str,
        script_name: str,
        args: list[str] | None = None,
        timeout_s: int = default_script_timeout_s,
        cleanup_workspace: bool = False,
    ) -> Any:
        """Legacy compatibility wrapper for explicit script execution."""
        return await service.execute_skill_script(
            skill_name=skill_name,
            script_name=script_name,
            args=args,
            timeout_s=timeout_s,
            cleanup_workspace=cleanup_workspace,
        )

    @mcp.resource("skill://catalog")
    def skill_catalog_resource() -> str:
        """Read-only skill catalog resource for clients that support resources/list/read."""
        return json.dumps(service.list_skills(), ensure_ascii=False, indent=2)

    @mcp.resource("skill://{skill_name}")
    def skill_resource(skill_name: str) -> str:
        """Read-only resource exposing one skill's coarse-grained payload."""
        return json.dumps(service.read_skill(skill_name), ensure_ascii=False, indent=2)

    @mcp.resource("skill://{skill_name}/docs")
    def skill_docs_resource(skill_name: str) -> str:
        """Compatibility resource exposing one skill's docs payload."""
        return json.dumps(service.read_skill_docs(skill_name), ensure_ascii=False, indent=2)

    @mcp.resource("skill://{skill_name}/files/{relative_path}")
    def skill_file_resource(skill_name: str, relative_path: str) -> str:
        """Read one skill file by relative path."""
        return json.dumps(
            service.read_skill_file(skill_name, relative_path),
            ensure_ascii=False,
            indent=2,
        )

    @mcp.resource("skill://{skill_name}/references/{reference_name}")
    def skill_reference_resource(skill_name: str, reference_name: str) -> str:
        """Read one reference file by relative path."""
        return service.read_skill_reference(skill_name, reference_name)

    @mcp.resource("skill-run://{run_id}/logs")
    def skill_run_logs_resource(run_id: str) -> str:
        """Read full logs for a completed shell-based skill run."""
        return json.dumps(service.get_run_logs(run_id), ensure_ascii=False, indent=2)

    @mcp.resource("skill-run://{run_id}/artifacts")
    def skill_run_artifacts_resource(run_id: str) -> str:
        """Read artifact metadata for a completed shell-based skill run."""
        return json.dumps(service.get_run_artifacts(run_id), ensure_ascii=False, indent=2)

    return TransportBindings(
        list_skills=list_skills,
        read_skill=read_skill,
        read_skill_file=read_skill_file,
        read_skill_docs=read_skill_docs,
        run_skill_task=run_skill_task,
        get_run_status=get_run_status,
        cancel_skill_run=cancel_skill_run,
        get_run_logs=get_run_logs,
        get_run_artifacts=get_run_artifacts,
        execute_skill_script=execute_skill_script,
        skill_catalog_resource=skill_catalog_resource,
        skill_resource=skill_resource,
        skill_docs_resource=skill_docs_resource,
        skill_file_resource=skill_file_resource,
        skill_reference_resource=skill_reference_resource,
        skill_run_logs_resource=skill_run_logs_resource,
        skill_run_artifacts_resource=skill_run_artifacts_resource,
    )
