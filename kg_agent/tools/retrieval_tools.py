from __future__ import annotations

from datetime import datetime, timezone
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
    search_query: str | None = None,
    mode: str = "hybrid",
    session_context: dict[str, Any] | None = None,
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    user_prompt: str | None = None,
    freshness_config=None,
    **_: Any,
) -> ToolResult:
    effective_query = (search_query or query or "").strip()
    param = QueryParam(mode=mode if mode in {"hybrid", "mix"} else "hybrid")
    if top_k is not None:
        param.top_k = top_k
    if chunk_top_k is not None:
        param.chunk_top_k = chunk_top_k
    if user_prompt:
        param.user_prompt = user_prompt
    if session_context and session_context.get("history"):
        param.conversation_history = session_context["history"]
    if freshness_config is not None:
        param.enable_freshness_decay = bool(
            getattr(freshness_config, "enable_freshness_decay", False)
        )
        param.staleness_decay_days = float(
            getattr(freshness_config, "staleness_decay_days", 7.0)
        )

    result = await rag.aquery_data(effective_query, param=param)
    _apply_freshness_decay(result, freshness_config)
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
    search_query: str | None = None,
    session_context: dict[str, Any] | None = None,
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    freshness_config=None,
    **_: Any,
) -> ToolResult:
    effective_query = (search_query or query or "").strip()
    param = QueryParam(mode="naive")
    if top_k is not None:
        param.top_k = top_k
    if chunk_top_k is not None:
        param.chunk_top_k = chunk_top_k
    if session_context and session_context.get("history"):
        param.conversation_history = session_context["history"]
    if freshness_config is not None:
        param.enable_freshness_decay = bool(
            getattr(freshness_config, "enable_freshness_decay", False)
        )
        param.staleness_decay_days = float(
            getattr(freshness_config, "staleness_decay_days", 7.0)
        )

    result = await rag.aquery_data(effective_query, param=param)
    _apply_freshness_decay(result, freshness_config)
    return ToolResult(
        tool_name="kg_naive_search",
        success=result.get("status") == "success",
        data={**result, "summary": _summarize_query_data(result)},
        error=None if result.get("status") == "success" else result.get("message"),
        metadata={"mode": "naive"},
    )


def _apply_freshness_decay(result: dict[str, Any], freshness_config: Any) -> None:
    if not isinstance(result, dict):
        return
    if freshness_config is None or not getattr(
        freshness_config, "enable_freshness_decay", False
    ):
        return
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata.get("freshness_decay_applied"):
        return

    data = result.get("data")
    if not isinstance(data, dict):
        return

    decay_days = max(0.1, float(getattr(freshness_config, "staleness_decay_days", 7.0)))
    now = datetime.now(timezone.utc)

    for key in ("entities", "relationships"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        items.sort(
            key=lambda item: _freshness_weighted_rank(item, now, decay_days),
            reverse=True,
        )


def _freshness_weighted_rank(
    item: Any,
    now: datetime,
    decay_days: float,
) -> float:
    if not isinstance(item, dict):
        return 0.0

    base_rank = item.get("rank")
    try:
        original_score = float(base_rank)
    except (TypeError, ValueError):
        original_score = 1.0

    freshness_value = item.get("last_confirmed_at")
    if not isinstance(freshness_value, str) or not freshness_value.strip():
        return original_score

    try:
        confirmed_at = datetime.fromisoformat(freshness_value)
    except ValueError:
        return original_score

    age_days = max(0.0, (now - confirmed_at).total_seconds() / 86400.0)
    freshness_score = 0.5 ** (age_days / decay_days)
    return original_score * (0.3 + 0.7 * freshness_score)
