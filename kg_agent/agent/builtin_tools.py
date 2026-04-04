from __future__ import annotations

from typing import Any

from kg_agent.agent.tool_registry import ToolRegistry
from kg_agent.agent.tool_schemas import (
    CROSS_SESSION_SEARCH_SCHEMA,
    GRAPH_ENTITY_LOOKUP_SCHEMA,
    GRAPH_RELATION_TRACE_SCHEMA,
    KG_HYBRID_SEARCH_SCHEMA,
    KG_INGEST_SCHEMA,
    KG_NAIVE_SEARCH_SCHEMA,
    MEMORY_SEARCH_SCHEMA,
    QUANT_BACKTEST_SCHEMA,
    WEB_SEARCH_SCHEMA,
)
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.memory.cross_session_store import CrossSessionStore
from kg_agent.tools.base import ToolDefinition, ToolResult
from kg_agent.tools.graph_tools import graph_entity_lookup, graph_relation_trace
from kg_agent.tools.quant_tools import quant_backtest
from kg_agent.tools.kg_ingest import kg_ingest
from kg_agent.tools.retrieval_tools import kg_hybrid_search, kg_naive_search
from kg_agent.tools.rerank_utils import (
    get_rerank_candidate_limit,
    get_rerank_settings,
    rerank_payloads,
)
from kg_agent.tools.web_search import web_search


async def memory_search(
    *,
    memory_store: ConversationMemoryStore | None,
    session_id: str,
    query: str,
    limit: int = 4,
    rag=None,
    **_: Any,
) -> ToolResult:
    if memory_store is None:
        return ToolResult(
            tool_name="memory_search",
            success=False,
            error="Memory store is not configured",
        )

    rerank_model_func, min_rerank_score = get_rerank_settings(rag)
    candidate_limit = get_rerank_candidate_limit(
        final_limit=limit,
        rerank_model_func=rerank_model_func,
    )
    matches = await memory_store.search(
        session_id=session_id,
        query=query,
        limit=candidate_limit,
    )
    matches, rerank_metadata = await rerank_payloads(
        query=query,
        payloads=matches,
        rerank_model_func=rerank_model_func,
        top_n=limit,
        min_rerank_score=min_rerank_score,
        content_fields=("content",),
    )
    return ToolResult(
        tool_name="memory_search",
        success=bool(matches),
        data={
            "matches": matches,
            "summary": f"Loaded {len(matches)} memory items from the current session",
        },
        error=None if matches else "No relevant memory found in the current session",
        metadata={"match_count": len(matches), **rerank_metadata},
    )


async def cross_session_search(
    *,
    cross_session_store: CrossSessionStore | None,
    user_id: str | None,
    session_id: str,
    query: str,
    limit: int = 5,
    rag=None,
    **_: Any,
) -> ToolResult:
    if cross_session_store is None:
        return ToolResult(
            tool_name="cross_session_search",
            success=False,
            error="Cross-session store is not configured",
        )

    rerank_model_func, min_rerank_score = get_rerank_settings(rag)
    candidate_limit = get_rerank_candidate_limit(
        final_limit=limit,
        rerank_model_func=rerank_model_func,
    )
    matches = await cross_session_store.search(
        user_id,
        query=query,
        limit=candidate_limit,
        exclude_session_id=session_id,
    )
    matches, rerank_metadata = await rerank_payloads(
        query=query,
        payloads=matches,
        rerank_model_func=rerank_model_func,
        top_n=limit,
        min_rerank_score=min_rerank_score,
        content_fields=("content", "summary"),
    )
    return ToolResult(
        tool_name="cross_session_search",
        success=bool(matches),
        data={
            "matches": matches,
            "summary": f"Loaded {len(matches)} memory items from prior sessions",
        },
        error=None if matches else "No relevant cross-session memory found",
        metadata={"match_count": len(matches), **rerank_metadata},
    )


def build_default_tool_registry(
    config,
    memory_store: ConversationMemoryStore,
    cross_session_store: CrossSessionStore | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            name="kg_hybrid_search",
            description="Run LightRAG hybrid retrieval and return structured entities, relations, and chunks.",
            input_schema=KG_HYBRID_SEARCH_SCHEMA,
            handler=kg_hybrid_search,
            tags=["retrieval", "knowledge-graph"],
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_naive_search",
            description="Run LightRAG vector-only retrieval and return structured chunk results.",
            input_schema=KG_NAIVE_SEARCH_SCHEMA,
            handler=kg_naive_search,
            tags=["retrieval", "vector"],
        )
    )
    registry.register(
        ToolDefinition(
            name="graph_entity_lookup",
            description="Look up a graph entity and return matched node details.",
            input_schema=GRAPH_ENTITY_LOOKUP_SCHEMA,
            handler=graph_entity_lookup,
            tags=["graph"],
        )
    )
    registry.register(
        ToolDefinition(
            name="graph_relation_trace",
            description="Trace short candidate paths around a graph entity for explanation.",
            input_schema=GRAPH_RELATION_TRACE_SCHEMA,
            handler=graph_relation_trace,
            tags=["graph", "explanation"],
        )
    )
    registry.register(
        ToolDefinition(
            name="memory_search",
            description="Search recent messages in the current session window.",
            input_schema=MEMORY_SEARCH_SCHEMA,
            handler=memory_search,
            enabled=config.tool_config.enable_memory,
            tags=["memory"],
        )
    )
    registry.register(
        ToolDefinition(
            name="cross_session_search",
            description="Search messages from the same user across prior sessions.",
            input_schema=CROSS_SESSION_SEARCH_SCHEMA,
            handler=cross_session_search,
            enabled=config.tool_config.enable_memory and cross_session_store is not None,
            tags=["memory", "cross-session"],
        )
    )
    registry.register(
        ToolDefinition(
            name="web_search",
            description="Crawl provided URLs and extract readable page content for time-sensitive questions.",
            input_schema=WEB_SEARCH_SCHEMA,
            handler=web_search,
            enabled=config.tool_config.enable_web_search,
            tags=["web"],
        )
    )
    registry.register(
        ToolDefinition(
            name="quant_backtest",
            description="Reserved tool entry for quant backtesting requests.",
            input_schema=QUANT_BACKTEST_SCHEMA,
            handler=quant_backtest,
            enabled=config.tool_config.enable_quant,
            tags=["quant"],
        )
    )
    registry.register(
        ToolDefinition(
            name="kg_ingest",
            description="Insert text or markdown content into the knowledge graph. Accepts content from any source (web page, PDF, manual input, etc.).",
            input_schema=KG_INGEST_SCHEMA,
            handler=kg_ingest,
            enabled=config.tool_config.enable_kg_ingest,
            tags=["knowledge-graph", "ingestion"],
        )
    )
    return registry
