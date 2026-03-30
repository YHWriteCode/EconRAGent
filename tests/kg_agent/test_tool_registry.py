import pytest

from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.tools.base import ToolDefinition, ToolResult


async def _echo_tool(query: str, **kwargs):
    return ToolResult(
        tool_name="echo",
        success=True,
        data={"query": query, "extra": kwargs},
    )


async def _boom_tool():
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_tool_registry_register_and_execute_success():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo tool",
            input_schema={},
            handler=_echo_tool,
        )
    )

    result = await registry.execute("echo", query="hello", unused="value")

    assert result.success is True
    assert result.data["query"] == "hello"


@pytest.mark.asyncio
async def test_tool_registry_execute_unknown_tool_returns_failure():
    registry = ToolRegistry()

    result = await registry.execute("missing", query="hello")

    assert result.success is False
    assert "not registered" in result.error.lower()


@pytest.mark.asyncio
async def test_tool_registry_execute_catches_handler_exception():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="boom",
            description="Boom tool",
            input_schema={},
            handler=_boom_tool,
        )
    )

    result = await registry.execute("boom")

    assert result.success is False
    assert result.metadata["exception_type"] == "RuntimeError"
