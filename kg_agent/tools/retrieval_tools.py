from __future__ import annotations

from typing import Any

from lightrag_fork.base import QueryParam

from kg_agent.tools.base import ToolResult


def _summarize_query_data(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    entities = len(data.get("entities", []))
    relationships = len(data.get("relationships", []))
    chunks = len(data.get("chunks", []))
    return (
        f"Retrieved {entities} entities, {relationships} relationships, and {chunks} chunks"
    )


async def kg_hybrid_search(
    *,
    rag,
    query: str,
    mode: str = "hybrid",
    session_context: dict[str, Any] | None = None,
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    user_prompt: str | None = None,
    **_: Any,
) -> ToolResult:
    param = QueryParam(mode=mode if mode in {"hybrid", "mix"} else "hybrid")
    if top_k is not None:
        param.top_k = top_k
    if chunk_top_k is not None:
        param.chunk_top_k = chunk_top_k
    if user_prompt:
        param.user_prompt = user_prompt
    if session_context and session_context.get("history"):
        param.conversation_history = session_context["history"]

    result = await rag.aquery_data(query, param=param)
    return ToolResult(
        tool_name="kg_hybrid_search",
        success=result.get("status") == "success",
        data={**result, "summary": _summarize_query_data(result)},
        error=None if result.get("status") == "success" else result.get("message"),
        metadata={"mode": param.mode},
    )


async def kg_naive_search(
    *,
    rag,
    query: str,
    session_context: dict[str, Any] | None = None,
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    **_: Any,
) -> ToolResult:
    param = QueryParam(mode="naive")
    if top_k is not None:
        param.top_k = top_k
    if chunk_top_k is not None:
        param.chunk_top_k = chunk_top_k
    if session_context and session_context.get("history"):
        param.conversation_history = session_context["history"]

    result = await rag.aquery_data(query, param=param)
    return ToolResult(
        tool_name="kg_naive_search",
        success=result.get("status") == "success",
        data={**result, "summary": _summarize_query_data(result)},
        error=None if result.get("status") == "success" else result.get("message"),
        metadata={"mode": "naive"},
    )
