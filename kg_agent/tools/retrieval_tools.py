from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lightrag_fork.base import QueryParam
from lightrag_fork.constants import GRAPH_FIELD_SEP
from lightrag_fork.utils import generate_reference_list_from_chunks

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
    crawl_state_store=None,
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
    await _apply_crawler_lifecycle_filter(
        result,
        rag=rag,
        crawl_state_store=crawl_state_store,
    )
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
    crawl_state_store=None,
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
    await _apply_crawler_lifecycle_filter(
        result,
        rag=rag,
        crawl_state_store=crawl_state_store,
    )
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


async def _apply_crawler_lifecycle_filter(
    result: dict[str, Any],
    *,
    rag: Any,
    crawl_state_store: Any,
) -> None:
    if not isinstance(result, dict) or crawl_state_store is None or rag is None:
        return

    payload = result.get("data")
    if not isinstance(payload, dict):
        return

    text_chunks = getattr(rag, "text_chunks", None)
    get_by_ids = getattr(text_chunks, "get_by_ids", None)
    if not callable(get_by_ids):
        return

    records = await crawl_state_store.list_records()
    lifecycle = _build_crawler_lifecycle_snapshot(records)
    if not lifecycle["managed_doc_ids"]:
        return

    chunk_ids = _collect_retrieval_chunk_ids(payload)
    if not chunk_ids:
        return

    stored_chunks = await get_by_ids(chunk_ids)
    chunk_lookup = _build_chunk_lookup(chunk_ids, stored_chunks)
    if not chunk_lookup:
        return

    filtered_chunks, filtered_chunk_count = _filter_chunk_payload(
        payload.get("chunks"),
        chunk_lookup=chunk_lookup,
        lifecycle=lifecycle,
    )
    filtered_entities, filtered_entity_count = _filter_graph_payload(
        payload.get("entities"),
        chunk_lookup=chunk_lookup,
        lifecycle=lifecycle,
    )
    filtered_relationships, filtered_relationship_count = _filter_graph_payload(
        payload.get("relationships"),
        chunk_lookup=chunk_lookup,
        lifecycle=lifecycle,
    )

    references, filtered_chunks = generate_reference_list_from_chunks(filtered_chunks)
    payload["chunks"] = filtered_chunks
    payload["references"] = references
    payload["entities"] = filtered_entities
    payload["relationships"] = filtered_relationships

    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        result["metadata"] = metadata
    metadata["crawler_lifecycle_filter"] = {
        "applied": True,
        "filtered_chunk_count": filtered_chunk_count,
        "filtered_entity_count": filtered_entity_count,
        "filtered_relationship_count": filtered_relationship_count,
        "active_short_term_doc_count": len(lifecycle["active_doc_ids"]),
        "expired_short_term_doc_count": len(lifecycle["expired_doc_ids"]),
    }


def _build_crawler_lifecycle_snapshot(records: list[Any]) -> dict[str, set[str]]:
    active_doc_ids: set[str] = set()
    expired_doc_ids: set[str] = set()
    managed_doc_ids: set[str] = set()
    now = datetime.now(timezone.utc)

    for record in records:
        item_active_doc_ids = getattr(record, "item_active_doc_ids", {})
        event_clusters = getattr(record, "event_clusters", {})
        doc_expires_at = getattr(record, "doc_expires_at", {})
        current_expired = _collect_expired_doc_ids(doc_expires_at, now=now)
        expired_doc_ids.update(current_expired)

        for doc_id in item_active_doc_ids.values():
            normalized_doc_id = str(doc_id or "").strip()
            if not normalized_doc_id:
                continue
            managed_doc_ids.add(normalized_doc_id)
            if normalized_doc_id not in current_expired:
                active_doc_ids.add(normalized_doc_id)
        for cluster_record in (
            event_clusters.values() if isinstance(event_clusters, dict) else []
        ):
            normalized_doc_id = str(
                (
                    cluster_record.get("active_doc_id")
                    if isinstance(cluster_record, dict)
                    else getattr(cluster_record, "active_doc_id", "")
                )
                or ""
            ).strip()
            if not normalized_doc_id:
                continue
            managed_doc_ids.add(normalized_doc_id)
            if normalized_doc_id not in current_expired:
                active_doc_ids.add(normalized_doc_id)

    managed_doc_ids.update(expired_doc_ids)
    return {
        "active_doc_ids": active_doc_ids,
        "expired_doc_ids": expired_doc_ids,
        "managed_doc_ids": managed_doc_ids,
    }


def _collect_expired_doc_ids(
    doc_expires_at: Any,
    *,
    now: datetime,
) -> set[str]:
    if not isinstance(doc_expires_at, dict):
        return set()

    expired: set[str] = set()
    for doc_id, expires_at in doc_expires_at.items():
        normalized_doc_id = str(doc_id or "").strip()
        expires_dt = _parse_iso8601(expires_at)
        if not normalized_doc_id or expires_dt is None or expires_dt > now:
            continue
        expired.add(normalized_doc_id)
    return expired


def _parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _collect_retrieval_chunk_ids(payload: dict[str, Any]) -> list[str]:
    chunk_ids: list[str] = []

    for chunk in payload.get("chunks", []):
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        if chunk_id:
            chunk_ids.append(chunk_id)

    for key in ("entities", "relationships"):
        for item in payload.get(key, []):
            if not isinstance(item, dict):
                continue
            chunk_ids.extend(_split_graph_field(item.get("source_id")))

    return _unique_preserve_order(chunk_ids)


def _build_chunk_lookup(
    requested_chunk_ids: list[str],
    stored_chunks: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(stored_chunks, list):
        return {}

    lookup: dict[str, dict[str, Any]] = {}
    for requested_id, stored_chunk in zip(requested_chunk_ids, stored_chunks):
        if not isinstance(stored_chunk, dict):
            continue
        normalized_id = str(stored_chunk.get("_id") or requested_id or "").strip()
        if not normalized_id:
            continue
        lookup[requested_id] = stored_chunk
        lookup[normalized_id] = stored_chunk
    return lookup


def _filter_chunk_payload(
    payload: Any,
    *,
    chunk_lookup: dict[str, dict[str, Any]],
    lifecycle: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, list):
        return [], 0

    filtered: list[dict[str, Any]] = []
    filtered_count = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "").strip()
        stored_chunk = chunk_lookup.get(chunk_id)
        if stored_chunk is not None:
            doc_id = str(stored_chunk.get("full_doc_id") or "").strip()
            if not _should_keep_doc_id(doc_id, lifecycle=lifecycle):
                filtered_count += 1
                continue
        filtered.append(dict(item))
    return filtered, filtered_count


def _filter_graph_payload(
    payload: Any,
    *,
    chunk_lookup: dict[str, dict[str, Any]],
    lifecycle: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, list):
        return [], 0

    filtered: list[dict[str, Any]] = []
    filtered_count = 0
    for item in payload:
        if not isinstance(item, dict):
            continue

        source_chunk_ids = _split_graph_field(item.get("source_id"))
        if not source_chunk_ids:
            filtered.append(dict(item))
            continue

        known_chunk_ids = [chunk_id for chunk_id in source_chunk_ids if chunk_id in chunk_lookup]
        if not known_chunk_ids:
            filtered.append(dict(item))
            continue

        allowed_chunk_ids: list[str] = []
        allowed_file_paths: list[str] = []
        for chunk_id in known_chunk_ids:
            stored_chunk = chunk_lookup.get(chunk_id) or {}
            doc_id = str(stored_chunk.get("full_doc_id") or "").strip()
            if not _should_keep_doc_id(doc_id, lifecycle=lifecycle):
                continue
            allowed_chunk_ids.append(chunk_id)
            file_path = str(stored_chunk.get("file_path") or "").strip()
            if file_path:
                allowed_file_paths.append(file_path)

        if not allowed_chunk_ids:
            filtered_count += 1
            continue

        updated = dict(item)
        updated["source_id"] = GRAPH_FIELD_SEP.join(
            _unique_preserve_order(allowed_chunk_ids)
        )
        if allowed_file_paths:
            updated["file_path"] = GRAPH_FIELD_SEP.join(
                _unique_preserve_order(allowed_file_paths)
            )
        filtered.append(updated)

    return filtered, filtered_count


def _split_graph_field(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return _unique_preserve_order(
        [item.strip() for item in value.split(GRAPH_FIELD_SEP) if item.strip()]
    )


def _unique_preserve_order(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _should_keep_doc_id(
    doc_id: str,
    *,
    lifecycle: dict[str, set[str]],
) -> bool:
    if not doc_id:
        return True
    if doc_id in lifecycle["expired_doc_ids"]:
        return False
    if doc_id in lifecycle["active_doc_ids"]:
        return True
    if doc_id in lifecycle["managed_doc_ids"]:
        return False
    return True
