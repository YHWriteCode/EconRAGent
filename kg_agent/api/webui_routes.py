from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator

from lightrag_fork.schema import resolve_domain_schema
from kg_agent.uploads import UploadStore
from kg_agent.workspace_registry import (
    WorkspaceRecord,
    WorkspaceRegistry,
    generate_workspace_id,
)


GRAPH_TIME_KEYS = ("last_confirmed_at", "updated_at", "created_at", "published_at")


class WorkspaceCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)
    description: str | None = None

    @field_validator("display_name", mode="after")
    @classmethod
    def strip_display_name(cls, value: str) -> str:
        return value.strip()


class WorkspaceUpdateRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None

    @field_validator("display_name", mode="after")
    @classmethod
    def strip_optional_display_name(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class WorkspaceImportRequest(BaseModel):
    kind: Literal["text", "url", "upload"]
    text: str | None = None
    url: str | None = None
    upload_id: str | None = None
    source: str | None = None


class GraphPathExplainRequest(BaseModel):
    workspace: str | None = None
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    max_depth: int = Field(default=3, ge=1, le=6)
    max_paths: int = Field(default=3, ge=1, le=8)
    domain_schema: str | dict[str, Any] | None = None


def _normalize_workspace(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    try:
        return value.timestamp()
    except Exception:
        return None


def _read_time_value(payload: dict[str, Any]) -> str | None:
    for key in GRAPH_TIME_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _within_time_window(
    payload: dict[str, Any],
    *,
    time_from: str | None,
    time_to: str | None,
) -> bool:
    if not time_from and not time_to:
        return True
    value_ts = _to_timestamp(_parse_iso8601(_read_time_value(payload)))
    if value_ts is None:
        return False
    start_ts = _to_timestamp(_parse_iso8601(time_from))
    end_ts = _to_timestamp(_parse_iso8601(time_to))
    if start_ts is not None and value_ts < start_ts:
        return False
    if end_ts is not None and value_ts > end_ts:
        return False
    return True


def _node_to_public(node: Any, workspace_id: str) -> dict[str, Any]:
    raw = node.model_dump() if hasattr(node, "model_dump") else dict(node)
    properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    return {
        "id": str(raw.get("id") or ""),
        "labels": list(raw.get("labels") or []),
        "properties": properties,
        "workspace_id": workspace_id,
    }


def _edge_to_public(edge: Any, workspace_id: str) -> dict[str, Any]:
    raw = edge.model_dump() if hasattr(edge, "model_dump") else dict(edge)
    properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    return {
        "id": str(raw.get("id") or ""),
        "type": raw.get("type"),
        "source": str(raw.get("source") or ""),
        "target": str(raw.get("target") or ""),
        "properties": properties,
        "workspace_id": workspace_id,
    }


def _filter_graph_payload(
    graph_payload: dict[str, Any],
    *,
    entity_type: str | None,
    relation_type: str | None,
    time_from: str | None,
    time_to: str | None,
) -> dict[str, Any]:
    normalized_entity_type = (entity_type or "").strip().lower()
    normalized_relation_type = _normalize_graph_filter_value(relation_type)
    nodes = []
    allowed_node_ids: set[tuple[str, str]] = set()
    for node in graph_payload.get("nodes", []):
        if not isinstance(node, dict):
            continue
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        current_entity_type = str(
            properties.get("entity_type")
            or properties.get("type")
            or ""
        ).strip().lower()
        if normalized_entity_type and current_entity_type != normalized_entity_type:
            continue
        if not _within_time_window(properties, time_from=time_from, time_to=time_to):
            continue
        nodes.append(node)
        allowed_node_ids.add((str(node.get("workspace_id") or ""), str(node.get("id") or "")))

    edges = []
    relation_node_ids: set[tuple[str, str]] = set()
    for edge in graph_payload.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source_key = (str(edge.get("workspace_id") or ""), str(edge.get("source") or ""))
        target_key = (str(edge.get("workspace_id") or ""), str(edge.get("target") or ""))
        if source_key not in allowed_node_ids or target_key not in allowed_node_ids:
            continue
        properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
        if not _within_time_window(properties, time_from=time_from, time_to=time_to):
            continue
        if normalized_relation_type and normalized_relation_type not in _edge_relation_tokens(edge):
            continue
        edges.append(edge)
        relation_node_ids.add(source_key)
        relation_node_ids.add(target_key)

    if normalized_relation_type:
        nodes = [
            node
            for node in nodes
            if (str(node.get("workspace_id") or ""), str(node.get("id") or ""))
            in relation_node_ids
        ]

    return {
        **graph_payload,
        "nodes": nodes,
        "edges": edges,
    }


def _normalize_graph_filter_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _split_graph_type_tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        tokens: set[str] = set()
        for item in value:
            tokens.update(_split_graph_type_tokens(item))
        return tokens
    normalized = str(value).replace("<SEP>", ",")
    for separator in ("，", "；", ";", "/", "|"):
        normalized = normalized.replace(separator, ",")
    return {
        token.strip().lower()
        for token in normalized.split(",")
        if token.strip()
    }


def _edge_relation_tokens(edge: dict[str, Any]) -> set[str]:
    properties = edge.get("properties") if isinstance(edge.get("properties"), dict) else {}
    tokens = _split_graph_type_tokens(edge.get("type"))
    for key in ("relation_type", "relation", "keywords"):
        tokens.update(_split_graph_type_tokens(properties.get(key)))
    return tokens


def _graph_summary(graph_payload: dict[str, Any]) -> dict[str, Any]:
    entity_types: dict[str, int] = {}
    for node in graph_payload.get("nodes", []):
        if not isinstance(node, dict):
            continue
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        entity_type = str(
            properties.get("entity_type")
            or properties.get("type")
            or "unknown"
        ).strip() or "unknown"
        entity_types[entity_type] = entity_types.get(entity_type, 0) + 1
    return {
        "node_count": len(graph_payload.get("nodes", [])),
        "edge_count": len(graph_payload.get("edges", [])),
        "entity_type_counts": entity_types,
        "is_truncated": bool(graph_payload.get("is_truncated", False)),
    }


def _schema_option(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or ""),
        "display_name": str(item.get("display_name") or item.get("name") or ""),
        "description": str(item.get("description") or ""),
        "aliases": list(item.get("aliases") or []),
    }


def _schema_payload_from_runtime_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_name": schema["profile_name"],
        "domain_name": schema["domain_name"],
        "entity_types": [
            _schema_option(item)
            for item in schema.get("entity_types", [])
            if str(item.get("name") or "").strip()
        ],
        "relation_types": [
            {
                **_schema_option(item),
                "source_types": list(item.get("source_types") or []),
                "target_types": list(item.get("target_types") or []),
            }
            for item in schema.get("relation_types", [])
            if str(item.get("name") or "").strip()
        ],
    }


def _configured_graph_schema_payload(agent_core: Any) -> dict[str, Any]:
    profile_name = "economy"
    config = getattr(agent_core, "config", None)
    runtime = getattr(config, "runtime", None)
    configured_profile = getattr(runtime, "default_domain_schema", None)
    if (
        isinstance(configured_profile, str)
        and configured_profile.strip()
        and configured_profile.strip() != "general"
    ):
        profile_name = configured_profile.strip()

    schema = resolve_domain_schema({"profile_name": profile_name}).to_runtime_dict()
    return _schema_payload_from_runtime_schema(schema)


async def _graph_schema_payload(
    *,
    agent_core: Any,
    workspace_registry: WorkspaceRegistry,
    workspace: str | None,
) -> dict[str, Any]:
    workspace_id = _normalize_workspace(workspace)
    candidate_workspaces: list[str] = []
    if workspace_id and workspace_id != "all":
        candidate_workspaces.append(workspace_id)
    else:
        records = await workspace_registry.list_workspaces()
        candidate_workspaces.extend(record.workspace_id for record in records)

    for candidate in candidate_workspaces:
        try:
            rag = await agent_core._resolve_rag(candidate)
        except Exception:
            continue
        addon_params = getattr(rag, "addon_params", None)
        if not isinstance(addon_params, dict):
            continue
        runtime_schema = addon_params.get("domain_schema")
        if not isinstance(runtime_schema, dict) or not runtime_schema.get("entity_types"):
            continue
        runtime_profile = str(runtime_schema.get("profile_name") or "").strip()
        if runtime_profile != "general" or runtime_schema.get("relation_types"):
            return _schema_payload_from_runtime_schema(runtime_schema)

    return _configured_graph_schema_payload(agent_core)


def _model_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dataclass_fields__"):
        dumped = asdict(value)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return (
        str(event.get("sort_time") or ""),
        str(event.get("event_id") or ""),
    )


def _build_cursor(event: dict[str, Any]) -> str:
    return f"{event.get('sort_time', '')}|{event.get('event_id', '')}"


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    normalized = (cursor or "").strip()
    if not normalized or "|" not in normalized:
        return None
    left, right = normalized.split("|", 1)
    return left, right


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _source_entry_from_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    domain = (parsed.netloc or "").strip().lower()
    scheme = parsed.scheme or "https"
    label = domain[:1].upper() if domain else "?"
    favicon_url = f"{scheme}://{domain}/favicon.ico" if domain else None
    return {
        "url": url,
        "domain": domain or None,
        "label": label,
        "favicon_url": favicon_url,
    }


def _is_expired_at(value: str | None) -> bool:
    expires_at = _parse_iso8601(value)
    expires_ts = _to_timestamp(expires_at)
    if expires_ts is None:
        return False
    return expires_ts <= datetime.now().timestamp()


async def _drop_workspace_data(rag: Any) -> None:
    storages = [
        getattr(rag, "full_docs", None),
        getattr(rag, "text_chunks", None),
        getattr(rag, "full_entities", None),
        getattr(rag, "full_relations", None),
        getattr(rag, "entity_chunks", None),
        getattr(rag, "relation_chunks", None),
        getattr(rag, "entities_vdb", None),
        getattr(rag, "relationships_vdb", None),
        getattr(rag, "chunks_vdb", None),
        getattr(rag, "chunk_entity_relation_graph", None),
        getattr(rag, "doc_status", None),
    ]
    for storage in storages:
        drop_func = getattr(storage, "drop", None)
        if callable(drop_func):
            result = drop_func()
            if asyncio.iscoroutine(result):
                await result


async def _build_workspace_stats(
    *,
    workspace_id: str,
    workspace_record: WorkspaceRecord,
    agent_core: Any,
    scheduler: Any,
) -> dict[str, Any]:
    rag = await agent_core._resolve_rag(workspace_id)
    labels_task = rag.get_graph_labels()
    status_counts_task = rag.doc_status.get_all_status_counts()
    latest_docs_task = rag.doc_status.get_docs_paginated(page=1, page_size=1)
    labels, status_counts, latest_docs = await asyncio.gather(
        labels_task,
        status_counts_task,
        latest_docs_task,
    )
    document_count = sum(
        int(value or 0)
        for value in (status_counts or {}).values()
    )
    node_count = len(labels or [])

    source_count = 0
    source_times: list[str] = []
    if scheduler is not None:
        sources = await scheduler.list_sources()
        filtered_sources = [
            source
            for source in sources
            if _normalize_workspace(getattr(source, "workspace", None)) == workspace_id
        ]
        source_count = len(filtered_sources)
        for source in filtered_sources:
            record = await scheduler.state_store.get_record(source.source_id)
            if record is not None and record.last_crawled_at:
                source_times.append(record.last_crawled_at)

    latest_doc_time = None
    if isinstance(latest_docs, tuple) and latest_docs[0]:
        _, doc_status = latest_docs[0][0]
        doc_status_payload = _model_to_dict(doc_status)
        updated_at = doc_status_payload.get("updated_at") or getattr(
            doc_status, "updated_at", None
        )
        if isinstance(updated_at, str) and updated_at.strip():
            latest_doc_time = updated_at.strip()
        elif updated_at is not None:
            latest_doc_time = str(updated_at)

    timestamps = [workspace_record.updated_at]
    if latest_doc_time:
        timestamps.append(latest_doc_time)
    timestamps.extend(source_times)
    last_updated_at = max(timestamps) if timestamps else workspace_record.updated_at

    return {
        "workspace_id": workspace_record.workspace_id,
        "display_name": workspace_record.display_name,
        "description": workspace_record.description,
        "created_at": workspace_record.created_at,
        "updated_at": workspace_record.updated_at,
        "document_count": document_count,
        "node_count": node_count,
        "source_count": source_count,
        "last_updated_at": last_updated_at,
        "archived": workspace_record.archived,
    }


async def _load_graph_payload(
    *,
    agent_core: Any,
    workspace_id: str,
    label: str,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    rag = await agent_core._resolve_rag(workspace_id)
    graph = await rag.get_knowledge_graph(
        node_label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    return {
        "workspace": workspace_id,
        "nodes": [_node_to_public(node, workspace_id) for node in graph.nodes],
        "edges": [_edge_to_public(edge, workspace_id) for edge in graph.edges],
        "is_truncated": bool(graph.is_truncated),
    }


def _budget_by_workspace(workspace_ids: list[str], max_nodes: int) -> dict[str, int]:
    if not workspace_ids or max_nodes <= 0:
        return {}
    base = max_nodes // len(workspace_ids)
    remainder = max_nodes % len(workspace_ids)
    return {
        workspace_id: base + (1 if index < remainder else 0)
        for index, workspace_id in enumerate(workspace_ids)
    }


def _merge_workspace_graph_payloads(
    payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ordered_workspaces = list(payloads.keys())
    return {
        "workspace": "all",
        "nodes": [
            node
            for workspace_id in ordered_workspaces
            for node in payloads[workspace_id].get("nodes", [])
        ],
        "edges": [
            edge
            for workspace_id in ordered_workspaces
            for edge in payloads[workspace_id].get("edges", [])
        ],
        "is_truncated": any(
            bool(payloads[workspace_id].get("is_truncated"))
            for workspace_id in ordered_workspaces
        ),
    }


async def _load_all_workspace_graph_payloads(
    *,
    agent_core: Any,
    workspace_ids: list[str],
    label: str,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    if not workspace_ids:
        return {
            "workspace": "all",
            "nodes": [],
            "edges": [],
            "is_truncated": False,
        }

    budgets = _budget_by_workspace(workspace_ids, max_nodes)
    payloads = {
        workspace_id: payload
        for workspace_id, payload in zip(
            workspace_ids,
            await asyncio.gather(
                *[
                    _load_graph_payload(
                        agent_core=agent_core,
                        workspace_id=workspace_id,
                        label=label,
                        max_depth=max_depth,
                        max_nodes=budgets[workspace_id],
                    )
                    for workspace_id in workspace_ids
                ]
            ),
            strict=False,
        )
    }

    while True:
        used_nodes = sum(len(payload.get("nodes", [])) for payload in payloads.values())
        remaining_budget = max_nodes - used_nodes
        expandable_workspaces = [
            workspace_id
            for workspace_id in workspace_ids
            if remaining_budget > 0 and bool(payloads[workspace_id].get("is_truncated"))
        ]
        if remaining_budget <= 0 or not expandable_workspaces:
            break

        extra_budgets = _budget_by_workspace(expandable_workspaces, remaining_budget)
        updated_payloads = {
            workspace_id: payload
            for workspace_id, payload in zip(
                expandable_workspaces,
                await asyncio.gather(
                    *[
                        _load_graph_payload(
                            agent_core=agent_core,
                            workspace_id=workspace_id,
                            label=label,
                            max_depth=max_depth,
                            max_nodes=budgets[workspace_id] + extra_budgets[workspace_id],
                        )
                        for workspace_id in expandable_workspaces
                    ]
                ),
                strict=False,
            )
        }

        growth = 0
        for workspace_id in expandable_workspaces:
            previous_count = len(payloads[workspace_id].get("nodes", []))
            new_count = len(updated_payloads[workspace_id].get("nodes", []))
            growth += max(0, new_count - previous_count)
            budgets[workspace_id] += extra_budgets[workspace_id]
            payloads[workspace_id] = updated_payloads[workspace_id]
        if growth == 0:
            break

    return _merge_workspace_graph_payloads(payloads)


async def _resolve_event_summary(
    *,
    agent_core: Any,
    workspace_id: str | None,
    cluster_record: Any,
) -> str:
    summary = str(getattr(cluster_record, "summary", "") or "").strip()
    if summary:
        return summary

    active_doc_id = str(getattr(cluster_record, "active_doc_id", "") or "").strip()
    workspace_key = _normalize_workspace(workspace_id)
    if active_doc_id and workspace_key:
        try:
            rag = await agent_core._resolve_rag(workspace_key)
            doc_status = await rag.doc_status.get_by_id(active_doc_id)
        except Exception:
            doc_status = None
        if isinstance(doc_status, dict):
            content_summary = str(doc_status.get("content_summary") or "").strip()
            if content_summary:
                return content_summary
        elif doc_status is not None:
            content_summary = str(getattr(doc_status, "content_summary", "") or "").strip()
            if content_summary:
                return content_summary
    headline = str(getattr(cluster_record, "headline", "") or "").strip()
    return headline


async def _build_event_cards(
    *,
    agent_core: Any,
    scheduler: Any,
    workspace: str | None,
    category: str | None,
) -> list[dict[str, Any]]:
    if scheduler is None:
        return []

    normalized_workspace = _normalize_workspace(workspace)
    normalized_category = (category or "").strip().lower()
    sources = await scheduler.list_sources()
    filtered_sources = []
    for source in sources:
        source_workspace = _normalize_workspace(getattr(source, "workspace", None))
        if normalized_workspace is not None and source_workspace != normalized_workspace:
            continue
        if normalized_category and str(getattr(source, "category", "")).strip().lower() != normalized_category:
            continue
        filtered_sources.append(source)

    events: list[dict[str, Any]] = []
    for source in filtered_sources:
        record = await scheduler.state_store.get_record(source.source_id)
        if record is None:
            continue
        expired_doc_ids = {
            doc_id
            for doc_id, expires_at in getattr(record, "doc_expires_at", {}).items()
            if _is_expired_at(str(expires_at or ""))
        }
        for cluster_id, cluster_record in getattr(record, "event_clusters", {}).items():
            active_doc_id = str(getattr(cluster_record, "active_doc_id", "") or "").strip()
            if active_doc_id and active_doc_id in expired_doc_ids:
                continue
            urls = _dedupe_preserve_order(
                list(getattr(cluster_record, "member_item_keys", []) or [])
                + [str(getattr(cluster_record, "representative_item_key", "") or "").strip()]
            )
            source_entries = [_source_entry_from_url(url) for url in urls if url]
            domains = _dedupe_preserve_order(
                [str(item.get("domain") or "") for item in source_entries if item.get("domain")]
            )
            sort_time = (
                str(getattr(cluster_record, "updated_at", "") or "").strip()
                or str(getattr(cluster_record, "published_at", "") or "").strip()
            )
            summary = await _resolve_event_summary(
                agent_core=agent_core,
                workspace_id=getattr(source, "workspace", None),
                cluster_record=cluster_record,
            )
            events.append(
                {
                    "event_id": f"{source.source_id}:{cluster_id}",
                    "workspace": _normalize_workspace(getattr(source, "workspace", None)),
                    "source_id": source.source_id,
                    "cluster_id": cluster_id,
                    "category": getattr(source, "category", None),
                    "headline": str(getattr(cluster_record, "headline", "") or "").strip(),
                    "summary": summary,
                    "published_at": getattr(cluster_record, "published_at", None),
                    "updated_at": getattr(cluster_record, "updated_at", None),
                    "sort_time": sort_time,
                    "source_count": len(domains),
                    "sources": source_entries,
                }
            )

    events.sort(key=_event_sort_key, reverse=True)
    return events


async def _find_event_by_id(
    *,
    agent_core: Any,
    scheduler: Any,
    event_id: str,
) -> dict[str, Any] | None:
    source_id, separator, cluster_id = event_id.partition(":")
    if not separator or scheduler is None:
        return None
    source = await scheduler.source_registry.get_source(source_id)
    if source is None:
        return None
    record = await scheduler.state_store.get_record(source_id)
    if record is None:
        return None
    cluster_record = getattr(record, "event_clusters", {}).get(cluster_id)
    if cluster_record is None:
        return None
    events = await _build_event_cards(
        agent_core=agent_core,
        scheduler=scheduler,
        workspace=getattr(source, "workspace", None),
        category=None,
    )
    for item in events:
        if item.get("event_id") == event_id:
            return item
    return None


async def _build_entity_detail(
    *,
    agent_core: Any,
    workspace_id: str,
    entity_id: str,
) -> dict[str, Any]:
    rag = await agent_core._resolve_rag(workspace_id)
    graph = rag.chunk_entity_relation_graph
    resolved_entity = entity_id
    if not await graph.has_node(resolved_entity):
        matches = await graph.search_labels(entity_id, 5)
        if not matches:
            raise HTTPException(status_code=404, detail="Entity not found")
        resolved_entity = matches[0]
    node = await graph.get_node(resolved_entity)
    if node is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    node_edges = await graph.get_node_edges(resolved_entity) or []
    neighbor_ids = _dedupe_preserve_order(
        [
            target if source == resolved_entity else source
            for source, target in node_edges
        ]
    )
    neighbor_nodes = await asyncio.gather(
        *[graph.get_node(neighbor_id) for neighbor_id in neighbor_ids]
    ) if neighbor_ids else []
    edge_payloads = await asyncio.gather(
        *[graph.get_edge(source, target) for source, target in node_edges]
    ) if node_edges else []
    return {
        "workspace": workspace_id,
        "entity_id": resolved_entity,
        "node": node,
        "neighbors": [
            {"entity_id": neighbor_id, "node": payload}
            for neighbor_id, payload in zip(neighbor_ids, neighbor_nodes)
            if payload is not None
        ],
        "relations": [
            {
                "source": source,
                "target": target,
                "edge": payload,
            }
            for (source, target), payload in zip(node_edges, edge_payloads)
            if payload is not None
        ],
    }


def _extract_paths_between_nodes(
    *,
    graph_payload: dict[str, Any],
    source: str,
    target: str,
    max_paths: int,
) -> list[dict[str, Any]]:
    node_lookup = {
        str(node.get("id") or ""): node
        for node in graph_payload.get("nodes", [])
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for edge in graph_payload.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("source") or "")
        tgt = str(edge.get("target") or "")
        adjacency.setdefault(src, []).append((tgt, edge))
        adjacency.setdefault(tgt, []).append((src, edge))

    queue: list[tuple[str, list[str], list[dict[str, Any]]]] = [(source, [source], [])]
    results: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    while queue and len(results) < max_paths:
        current, node_path, edge_path = queue.pop(0)
        if current == target and edge_path:
            key = tuple(node_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            results.append(
                {
                    "path_text": " -> ".join(node_path),
                    "nodes": [node_lookup[node_id] for node_id in node_path if node_id in node_lookup],
                    "edges": edge_path,
                }
            )
            continue
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in node_path:
                continue
            queue.append((neighbor, [*node_path, neighbor], [*edge_path, edge]))
    return results


def create_webui_routes(
    agent_core: Any,
    *,
    scheduler: Any = None,
    workspace_registry: WorkspaceRegistry | None = None,
    upload_store: UploadStore | None = None,
    rag_provider: Any = None,
):
    router = APIRouter(tags=["webui"])

    if workspace_registry is None:
        raise RuntimeError("workspace_registry is required for webui routes")
    if upload_store is None:
        raise RuntimeError("upload_store is required for webui routes")

    @router.post("/agent/uploads")
    async def create_upload(file: UploadFile = File(...)):
        payload = await file.read()
        record = await upload_store.save_upload(
            filename=file.filename or "upload.bin",
            content=payload,
            content_type=file.content_type,
        )
        return {
            "upload_id": record.upload_id,
            "upload": record.to_dict(),
        }

    @router.get("/agent/uploads/{upload_id}")
    async def get_upload(upload_id: str):
        record = await upload_store.get_upload(upload_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Upload not found")
        return record.to_dict()

    @router.get("/agent/sessions")
    async def list_sessions(
        user_id: str | None = None,
        workspace: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        sessions = await agent_core.conversation_memory.list_sessions(
            user_id=user_id,
            workspace=workspace,
            limit=limit,
        )
        return {"sessions": sessions}

    @router.get("/agent/sessions/{session_id}/messages")
    async def get_session_messages(session_id: str):
        messages = await agent_core.conversation_memory.list_messages(session_id)
        return {
            "session_id": session_id,
            "messages": messages,
        }

    @router.delete("/agent/sessions/{session_id}")
    async def delete_session(session_id: str):
        await agent_core.conversation_memory.clear_session(session_id)
        return {
            "status": "deleted",
            "session_id": session_id,
        }

    @router.get("/agent/workspaces")
    async def list_workspaces():
        workspaces = await workspace_registry.list_workspaces()
        items = [
            await _build_workspace_stats(
                workspace_id=record.workspace_id,
                workspace_record=record,
                agent_core=agent_core,
                scheduler=scheduler,
            )
            for record in workspaces
        ]
        return {"workspaces": items}

    @router.get("/agent/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str):
        record = await workspace_registry.get_workspace(workspace_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        return await _build_workspace_stats(
            workspace_id=workspace_id,
            workspace_record=record,
            agent_core=agent_core,
            scheduler=scheduler,
        )

    @router.post("/agent/workspaces")
    async def create_workspace(request: WorkspaceCreateRequest):
        workspace_id = generate_workspace_id(request.display_name)
        now = datetime.now().astimezone().isoformat()
        record = WorkspaceRecord(
            workspace_id=workspace_id,
            display_name=request.display_name,
            description=(request.description or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        stored = await workspace_registry.upsert_workspace(record)
        await agent_core._resolve_rag(stored.workspace_id)
        return await _build_workspace_stats(
            workspace_id=stored.workspace_id,
            workspace_record=stored,
            agent_core=agent_core,
            scheduler=scheduler,
        )

    @router.patch("/agent/workspaces/{workspace_id}")
    async def update_workspace(workspace_id: str, request: WorkspaceUpdateRequest):
        current = await workspace_registry.get_workspace(workspace_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        updated = WorkspaceRecord(
            workspace_id=current.workspace_id,
            display_name=request.display_name or current.display_name,
            description=(
                current.description
                if request.description is None
                else ((request.description or "").strip() or None)
            ),
            created_at=current.created_at,
            updated_at=datetime.now().astimezone().isoformat(),
            archived=current.archived,
        )
        stored = await workspace_registry.upsert_workspace(updated)
        return await _build_workspace_stats(
            workspace_id=stored.workspace_id,
            workspace_record=stored,
            agent_core=agent_core,
            scheduler=scheduler,
        )

    @router.delete("/agent/workspaces/{workspace_id}")
    async def delete_workspace(workspace_id: str):
        current = await workspace_registry.get_workspace(workspace_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Workspace not found")

        if scheduler is not None:
            sources = await scheduler.list_sources()
            for source in sources:
                if _normalize_workspace(getattr(source, "workspace", None)) != workspace_id:
                    continue
                await scheduler.remove_source(source.source_id)

        dropped = False
        if rag_provider is not None and callable(getattr(rag_provider, "drop_workspace", None)):
            await rag_provider.drop_workspace(workspace_id)
            dropped = True
        if not dropped:
            rag = await agent_core._resolve_rag(workspace_id)
            await _drop_workspace_data(rag)

        await workspace_registry.remove_workspace(workspace_id)
        return {
            "status": "deleted",
            "workspace_id": workspace_id,
        }

    @router.post("/agent/workspaces/{workspace_id}/imports")
    async def create_workspace_import(workspace_id: str, request: WorkspaceImportRequest):
        record = await workspace_registry.get_workspace(workspace_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        rag = await agent_core._resolve_rag(workspace_id)

        if request.kind == "text":
            text = (request.text or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="text import requires non-empty text")
            track_id = await rag.ainsert(
                input=text,
                file_paths=request.source or f"workspace:{workspace_id}:text-import",
            )
        elif request.kind == "url":
            url = (request.url or "").strip()
            if not url:
                raise HTTPException(status_code=400, detail="url import requires a URL")
            crawler_adapter = getattr(agent_core, "crawler_adapter", None)
            if crawler_adapter is None:
                raise HTTPException(status_code=503, detail="Crawler is not configured")
            page = await crawler_adapter.crawl_url(url)
            if not page.success or not page.markdown.strip():
                raise HTTPException(status_code=400, detail=page.error or "Failed to extract URL content")
            track_id = await rag.ainsert(
                input=page.markdown,
                file_paths=request.source or page.final_url or url,
            )
        else:
            upload_id = (request.upload_id or "").strip()
            if not upload_id:
                raise HTTPException(status_code=400, detail="upload import requires upload_id")
            upload_record = await upload_store.get_upload(upload_id)
            if upload_record is None:
                raise HTTPException(status_code=404, detail="Upload not found")
            try:
                extracted_text = await upload_store.read_extracted_text(upload_id)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if not extracted_text.strip():
                raise HTTPException(status_code=400, detail="Upload did not yield importable text")
            track_id = await rag.ainsert(
                input=extracted_text,
                file_paths=request.source or upload_record.filename,
            )

        return {
            "status": "accepted",
            "workspace_id": workspace_id,
            "track_id": track_id,
        }

    @router.get("/agent/imports/{track_id}")
    async def get_import_status(track_id: str, workspace: str | None = None):
        search_workspaces: list[str] = []
        normalized_workspace = _normalize_workspace(workspace)
        if normalized_workspace is not None:
            search_workspaces.append(normalized_workspace)
        else:
            search_workspaces.extend(
                [
                    record.workspace_id
                    for record in await workspace_registry.list_workspaces()
                ]
            )
        search_workspaces = _dedupe_preserve_order(search_workspaces)
        for workspace_id in search_workspaces:
            rag = await agent_core._resolve_rag(workspace_id)
            docs = await rag.aget_docs_by_track_id(track_id)
            if not docs:
                continue
            documents = []
            status_counts: dict[str, int] = {}
            for doc_id, doc_status in docs.items():
                payload = _model_to_dict(doc_status)
                status_key = str(payload.get("status") or "unknown")
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
                documents.append(
                    {
                        "doc_id": doc_id,
                        **payload,
                    }
                )
            return {
                "track_id": track_id,
                "workspace_id": workspace_id,
                "document_count": len(documents),
                "status_counts": status_counts,
                "documents": documents,
            }
        raise HTTPException(status_code=404, detail="Import track_id not found")

    @router.get("/agent/graph/overview")
    async def get_graph_overview(
        workspace: str = "all",
        max_nodes: int = Query(default=800, ge=1, le=800),
    ):
        normalized_workspace = (workspace or "all").strip() or "all"
        if normalized_workspace == "all":
            workspaces = await workspace_registry.list_workspaces()
            workspace_ids = [record.workspace_id for record in workspaces]
            if not workspace_ids:
                return {
                    "workspace": "all",
                    "nodes": [],
                    "edges": [],
                    "summary": _graph_summary({"nodes": [], "edges": [], "is_truncated": False}),
                    "is_truncated": False,
                }
            merged = await _load_all_workspace_graph_payloads(
                agent_core=agent_core,
                workspace_ids=workspace_ids,
                label="*",
                max_depth=2,
                max_nodes=max_nodes,
            )
            return {
                **merged,
                "summary": _graph_summary(merged),
            }

        payload = await _load_graph_payload(
            agent_core=agent_core,
            workspace_id=normalized_workspace,
            label="*",
            max_depth=2,
            max_nodes=max_nodes,
        )
        return {
            **payload,
            "summary": _graph_summary(payload),
        }

    @router.get("/agent/graph/schema")
    async def get_graph_schema(workspace: str = "all"):
        return await _graph_schema_payload(
            agent_core=agent_core,
            workspace_registry=workspace_registry,
            workspace=workspace,
        )

    @router.get("/agent/graph/subgraph")
    async def get_graph_subgraph(
        workspace: str,
        label: str = "*",
        max_depth: int = Query(default=2, ge=1, le=6),
        max_nodes: int = Query(default=800, ge=1, le=800),
        entity_type: str | None = None,
        relation_type: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
    ):
        workspace_id = _normalize_workspace(workspace)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace is required")
        graph_label = (label or "*").strip() or "*"
        if workspace_id == "all":
            records = await workspace_registry.list_workspaces()
            workspace_ids = [record.workspace_id for record in records]
            if not workspace_ids:
                payload = {
                    "workspace": "all",
                    "nodes": [],
                    "edges": [],
                    "is_truncated": False,
                }
            else:
                payload = await _load_all_workspace_graph_payloads(
                    agent_core=agent_core,
                    workspace_ids=workspace_ids,
                    label=graph_label,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                )
        else:
            payload = await _load_graph_payload(
                agent_core=agent_core,
                workspace_id=workspace_id,
                label=graph_label,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        filtered = _filter_graph_payload(
            payload,
            entity_type=entity_type,
            relation_type=relation_type,
            time_from=time_from,
            time_to=time_to,
        )
        return {
            **filtered,
            "summary": _graph_summary(filtered),
        }

    @router.get("/agent/graph/labels")
    async def search_graph_labels(
        workspace: str | None = None,
        q: str = "",
        limit: int = Query(default=20, ge=1, le=200),
    ):
        normalized_workspace = _normalize_workspace(workspace)
        if normalized_workspace == "all":
            records = await workspace_registry.list_workspaces()
            items: list[dict[str, Any]] = []
            for record in records:
                rag = await agent_core._resolve_rag(record.workspace_id)
                labels = await rag.chunk_entity_relation_graph.search_labels(q, limit)
                items.extend(
                    {
                        "workspace_id": record.workspace_id,
                        "label": label,
                    }
                    for label in labels
                )
            deduped = []
            seen: set[tuple[str, str]] = set()
            for item in items:
                key = (str(item.get("workspace_id") or ""), str(item.get("label") or ""))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            return {"items": deduped[:limit]}

        workspace_id = normalized_workspace
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace is required")
        rag = await agent_core._resolve_rag(workspace_id)
        labels = await rag.chunk_entity_relation_graph.search_labels(q, limit)
        return {
            "items": [
                {
                    "workspace_id": workspace_id,
                    "label": label,
                }
                for label in labels
            ]
        }

    @router.get("/agent/graph/entities/{entity_id}")
    async def get_graph_entity(entity_id: str, workspace: str):
        workspace_id = _normalize_workspace(workspace)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace is required")
        return await _build_entity_detail(
            agent_core=agent_core,
            workspace_id=workspace_id,
            entity_id=entity_id,
        )

    @router.get("/agent/graph/relations")
    async def get_graph_relation(workspace: str, source: str, target: str):
        workspace_id = _normalize_workspace(workspace)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace is required")
        rag = await agent_core._resolve_rag(workspace_id)
        edge = await rag.chunk_entity_relation_graph.get_edge(source, target)
        if edge is None:
            raise HTTPException(status_code=404, detail="Relation not found")
        source_node, target_node = await asyncio.gather(
            rag.chunk_entity_relation_graph.get_node(source),
            rag.chunk_entity_relation_graph.get_node(target),
        )
        return {
            "workspace": workspace_id,
            "source": source,
            "target": target,
            "edge": edge,
            "source_node": source_node,
            "target_node": target_node,
        }

    @router.post("/agent/graph/path_explain")
    async def explain_graph_path(request: GraphPathExplainRequest):
        workspace_id = _normalize_workspace(request.workspace)
        if workspace_id is None:
            raise HTTPException(status_code=400, detail="workspace is required")
        payload = await _load_graph_payload(
            agent_core=agent_core,
            workspace_id=workspace_id,
            label=request.source,
            max_depth=request.max_depth,
            max_nodes=160,
        )
        paths = _extract_paths_between_nodes(
            graph_payload=payload,
            source=request.source,
            target=request.target,
            max_paths=request.max_paths,
        )
        explanation = None
        if paths:
            explanation_obj = await agent_core.path_explainer.explain(
                query=f"{request.source} 与 {request.target} 的关系",
                graph_paths=paths,
                evidence_chunks=[],
                domain_schema=request.domain_schema
                if isinstance(request.domain_schema, dict)
                else {"profile_name": request.domain_schema},
            )
            explanation = asdict(explanation_obj)
        return {
            "workspace": workspace_id,
            "source": request.source,
            "target": request.target,
            "paths": paths,
            "path_explanation": explanation,
        }

    @router.get("/agent/discover/events")
    async def list_discover_events(
        workspace: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        category: str | None = None,
    ):
        events = await _build_event_cards(
            agent_core=agent_core,
            scheduler=scheduler,
            workspace=workspace,
            category=category,
        )
        cursor_key = _parse_cursor(cursor)
        filtered = []
        for event in events:
            event_key = _event_sort_key(event)
            if cursor_key is not None and event_key >= cursor_key:
                continue
            filtered.append(event)
            if len(filtered) >= limit:
                break
        next_cursor = _build_cursor(filtered[-1]) if filtered and len(filtered) == limit else None
        return {
            "items": filtered,
            "next_cursor": next_cursor,
        }

    @router.get("/agent/discover/events/{event_id}")
    async def get_discover_event(event_id: str):
        event = await _find_event_by_id(
            agent_core=agent_core,
            scheduler=scheduler,
            event_id=event_id,
        )
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return event

    @router.get("/agent/discover/sources")
    async def list_discover_sources(workspace: str | None = None):
        if scheduler is None:
            return {"sources": []}
        normalized_workspace = _normalize_workspace(workspace)
        sources = await scheduler.list_sources()
        items = []
        for source in sources:
            source_workspace = _normalize_workspace(getattr(source, "workspace", None))
            if normalized_workspace is not None and source_workspace != normalized_workspace:
                continue
            items.append(
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "workspace": source_workspace,
                    "category": source.category,
                    "urls": list(source.urls),
                }
            )
        return {"sources": items}

    return router
