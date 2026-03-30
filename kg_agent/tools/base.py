from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.success:
            if isinstance(self.data, dict):
                if "summary" in self.data and isinstance(self.data["summary"], str):
                    return self.data["summary"]
                if "message" in self.data and isinstance(self.data["message"], str):
                    return self.data["message"]
                if "status" in self.data and isinstance(self.data["status"], str):
                    return self.data["status"]
            return f"{self.tool_name} executed successfully"
        return self.error or f"{self.tool_name} failed"


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    enabled: bool = True
    tags: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "enabled": self.enabled,
            "tags": list(self.tags),
        }
