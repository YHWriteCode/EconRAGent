from __future__ import annotations

import inspect
from typing import Any

from kg_agent.tools.base import ToolDefinition, ToolResult


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool and tool.enabled)

    def list_tools(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in sorted(self._tools)]

    async def execute(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"Tool not registered: {name}",
            )
        if not tool.enabled:
            return ToolResult(
                tool_name=name,
                success=False,
                error=f"Tool is disabled: {name}",
            )

        try:
            filtered_kwargs = self._filter_kwargs(tool.handler, kwargs)
            result = await tool.handler(**filtered_kwargs)
        except Exception as exc:  # pragma: no cover
            return ToolResult(
                tool_name=name,
                success=False,
                error=str(exc),
                metadata={"exception_type": type(exc).__name__},
            )

        if result.tool_name != name:
            result.tool_name = name
        return result

    @staticmethod
    def _filter_kwargs(handler, kwargs: dict[str, Any]) -> dict[str, Any]:
        signature = inspect.signature(handler)
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            return kwargs

        allowed = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in allowed}
