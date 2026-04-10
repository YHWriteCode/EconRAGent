from kg_agent.agent.agent_core import AgentCore
from kg_agent.agent.capability_registry import build_native_capability_registry
from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.config import AgentModelConfig, KGAgentConfig
from kg_agent.tools.base import ToolDefinition, ToolResult


class _FakeRAG:
    workspace = ""


async def _echo_tool(query: str, **kwargs):
    return ToolResult(tool_name="echo", success=True, data={"query": query})


def test_build_native_capability_registry_mirrors_tool_registry():
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolDefinition(
            name="echo",
            description="Echo capability",
            input_schema={"type": "object"},
            handler=_echo_tool,
            tags=["test"],
        )
    )

    capability_registry = build_native_capability_registry(tool_registry)
    capabilities = capability_registry.list_capabilities()

    assert len(capabilities) == 1
    assert capabilities[0].name == "echo"
    assert capabilities[0].kind == "native"
    assert capabilities[0].executor == "tool_registry"
    assert capabilities[0].target_name == "echo"


def test_agent_core_list_tools_returns_capability_metadata():
    tool_registry = ToolRegistry()
    tool_registry.register(
        ToolDefinition(
            name="echo",
            description="Echo capability",
            input_schema={"type": "object"},
            handler=_echo_tool,
            tags=["test"],
        )
    )
    agent = AgentCore(
        rag=_FakeRAG(),
        config=KGAgentConfig(agent_model=AgentModelConfig(provider="disabled")),
        tool_registry=tool_registry,
    )

    listed = agent.list_tools()

    assert listed == [
        {
            "name": "echo",
            "description": "Echo capability",
            "input_schema": {"type": "object"},
            "enabled": True,
            "tags": ["test"],
            "kind": "native",
            "executor": "tool_registry",
        }
    ]
