from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kg_agent.config import MCPCapabilityConfig
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.tools.base import ToolDefinition


@dataclass
class CapabilityDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    kind: str = "native"
    executor: str = "tool_registry"
    target_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "enabled": self.enabled,
            "tags": list(self.tags),
            "kind": self.kind,
            "executor": self.executor,
        }

    @classmethod
    def from_tool_definition(cls, tool: ToolDefinition) -> "CapabilityDefinition":
        return cls(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            enabled=tool.enabled,
            tags=list(tool.tags),
            kind="native",
            executor="tool_registry",
            target_name=tool.name,
        )

    @classmethod
    def from_mcp_capability_config(
        cls,
        capability: MCPCapabilityConfig,
    ) -> "CapabilityDefinition":
        return cls(
            name=capability.name,
            description=capability.description,
            input_schema=capability.input_schema,
            enabled=capability.enabled,
            tags=list(capability.tags),
            kind="external_mcp",
            executor="mcp",
            target_name=capability.remote_name or capability.name,
            metadata={
                "server": capability.server,
                "planner_exposed": capability.planner_exposed,
            },
        )


class CapabilityRegistry:
    def __init__(self):
        self._capabilities: dict[str, CapabilityDefinition] = {}

    def register(self, capability: CapabilityDefinition) -> None:
        if capability.name in self._capabilities:
            raise ValueError(f"Capability already registered: {capability.name}")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> CapabilityDefinition | None:
        return self._capabilities.get(name)

    def has(self, name: str) -> bool:
        capability = self.get(name)
        return bool(capability and capability.enabled)

    def list_capabilities(self) -> list[CapabilityDefinition]:
        return [self._capabilities[name] for name in sorted(self._capabilities)]


def build_native_capability_registry(tool_registry: ToolRegistry) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for tool in tool_registry.list_tools():
        registry.register(CapabilityDefinition.from_tool_definition(tool))
    return registry


def add_mcp_capabilities(
    registry: CapabilityRegistry,
    capabilities: list[MCPCapabilityConfig],
    *,
    skip_existing: bool = False,
) -> CapabilityRegistry:
    for capability in capabilities:
        if skip_existing and registry.get(capability.name) is not None:
            continue
        registry.register(CapabilityDefinition.from_mcp_capability_config(capability))
    return registry
