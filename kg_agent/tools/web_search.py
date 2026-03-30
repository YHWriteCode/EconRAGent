from __future__ import annotations

from typing import Any

from kg_agent.tools.base import ToolResult


async def web_search(
    *,
    query: str,
    top_k: int = 5,
    **_: Any,
) -> ToolResult:
    return ToolResult(
        tool_name="web_search",
        success=False,
        data={
            "status": "not_configured",
            "query": query,
            "top_k": top_k,
            "summary": "Web search provider is not configured yet",
        },
        error="Web search provider is not configured in stage one",
        metadata={"implemented": False},
    )
