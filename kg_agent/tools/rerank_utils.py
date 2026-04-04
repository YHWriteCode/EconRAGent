from __future__ import annotations

import json
from typing import Any, Callable, Sequence


def get_rerank_settings(rag: Any) -> tuple[Callable[..., Any] | None, float]:
    rerank_model_func = getattr(rag, "rerank_model_func", None)
    try:
        min_rerank_score = float(getattr(rag, "min_rerank_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_rerank_score = 0.0
    return rerank_model_func, max(0.0, min_rerank_score)


def get_rerank_candidate_limit(
    *,
    final_limit: int,
    rerank_model_func: Callable[..., Any] | None,
    available_count: int | None = None,
    multiplier: int = 3,
    ceiling: int = 12,
) -> int:
    resolved_limit = max(1, int(final_limit or 1))
    if rerank_model_func is None:
        candidate_limit = resolved_limit
    else:
        candidate_limit = max(resolved_limit, min(resolved_limit * multiplier, ceiling))
    if available_count is not None:
        candidate_limit = min(candidate_limit, max(1, int(available_count)))
    return max(1, candidate_limit)


def _build_rerank_document(
    payload: dict[str, Any],
    *,
    content_fields: Sequence[str],
) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for field_name in content_fields:
        value = payload.get(field_name)
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parts.append(normalized)
    if parts:
        return "\n\n".join(parts)
    return json.dumps(payload, ensure_ascii=False, default=str)


async def rerank_payloads(
    *,
    query: str,
    payloads: list[dict[str, Any]],
    rerank_model_func: Callable[..., Any] | None,
    top_n: int,
    min_rerank_score: float = 0.0,
    content_fields: Sequence[str] = (
        "content",
        "markdown",
        "excerpt",
        "title",
        "summary",
        "text",
    ),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [dict(item) for item in payloads if isinstance(item, dict)]
    metadata = {
        "rerank_applied": False,
        "rerank_input_count": len(records),
        "rerank_output_count": len(records),
        "rerank_filtered_count": 0,
    }
    normalized_query = (query or "").strip()
    if rerank_model_func is None or not normalized_query or not records:
        return records[:top_n], metadata

    try:
        rerank_results = await rerank_model_func(
            query=normalized_query,
            documents=[
                _build_rerank_document(item, content_fields=content_fields)
                for item in records
            ],
            top_n=max(1, int(top_n or len(records))),
        )
    except Exception as exc:
        metadata["rerank_error"] = str(exc)
        return records[:top_n], metadata

    reranked_records: list[dict[str, Any]] = []
    if (
        isinstance(rerank_results, list)
        and rerank_results
        and isinstance(rerank_results[0], dict)
        and "index" in rerank_results[0]
    ):
        for result in rerank_results:
            index = result.get("index")
            if not isinstance(index, int) or not (0 <= index < len(records)):
                continue
            record = dict(records[index])
            try:
                record["rerank_score"] = float(result.get("relevance_score", 0.0))
            except (TypeError, ValueError):
                pass
            reranked_records.append(record)
    elif isinstance(rerank_results, list):
        reranked_records = [
            dict(item) for item in rerank_results if isinstance(item, dict)
        ]

    if not reranked_records:
        metadata["rerank_error"] = "Rerank returned no usable results"
        return records[:top_n], metadata

    filtered_records = reranked_records
    if min_rerank_score > 0.0:
        filtered_records = [
            item
            for item in reranked_records
            if float(item.get("rerank_score", 1.0)) >= min_rerank_score
        ]
        metadata["rerank_filtered_count"] = len(reranked_records) - len(filtered_records)

    filtered_records = filtered_records[:top_n]
    metadata["rerank_applied"] = True
    metadata["rerank_output_count"] = len(filtered_records)
    return filtered_records, metadata
