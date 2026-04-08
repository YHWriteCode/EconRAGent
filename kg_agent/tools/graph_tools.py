
from __future__ import annotations

from collections import defaultdict
from typing import Any

from kg_agent.tools.base import ToolResult


async def graph_entity_lookup(
    *,
    rag,
    query: str,
    entity_name: str | None = None,
    search_limit: int = 5,
    **_: Any,
) -> ToolResult:
    graph = rag.chunk_entity_relation_graph
    target = (entity_name or query).strip()
    candidate_labels: list[str] = []

    if await graph.has_node(target):
        candidate_labels = [target]
    else:
        candidate_labels = await graph.search_labels(target, search_limit)
        if not candidate_labels:
            return ToolResult(
                tool_name="graph_entity_lookup",
                success=False,
                error=f"No matching entity found for '{target}'",
            )

    resolved = candidate_labels[0]
    node_data = await graph.get_node(resolved)
    if node_data is None:
        return ToolResult(
            tool_name="graph_entity_lookup",
            success=False,
            error=f"Entity '{resolved}' exists in label search but could not be loaded",
        )

    return ToolResult(
        tool_name="graph_entity_lookup",
        success=True,
        data={
            "entity_name": resolved,
            "node": node_data,
            "candidate_labels": candidate_labels,
            "summary": f"Resolved entity '{resolved}' with {len(candidate_labels)} candidate labels",
        },
        metadata={"candidate_count": len(candidate_labels)},
    )


def _edge_lookup(edges: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge.get("source"), edge.get("target"))
        reverse_key = (edge.get("target"), edge.get("source"))
        mapping[key] = edge
        mapping.setdefault(reverse_key, edge)
    return mapping


async def graph_relation_trace(
    *,
    rag,
    query: str,
    entity_name: str | None = None,
    max_depth: int = 2,
    max_paths: int = 3,
    **_: Any,
) -> ToolResult:
    graph_storage = rag.chunk_entity_relation_graph
    target = (entity_name or query).strip()
    if not await graph_storage.has_node(target):
        candidates = await graph_storage.search_labels(target, 5)
        if not candidates:
            return ToolResult(
                tool_name="graph_relation_trace",
                success=False,
                error=f"No graph seed entity found for '{target}'",
            )
        target = candidates[0]

    kg = await rag.get_knowledge_graph(
        node_label=target, max_depth=max_depth, max_nodes=30
    )
    graph_dict = kg.model_dump()
    nodes = graph_dict.get("nodes", [])
    edges = graph_dict.get("edges", [])
    if not nodes:
        return ToolResult(
            tool_name="graph_relation_trace",
            success=False,
            error=f"No graph paths found for '{target}'",
        )

    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        source = edge.get("source")
        target_node = edge.get("target")
        if source and target_node:
            adjacency[source].add(target_node)
            adjacency[target_node].add(source)

    node_map = {node.get("id"): node for node in nodes}
    edge_map = _edge_lookup(edges)
    paths: list[dict[str, Any]] = []

    for neighbor in adjacency.get(target, set()):
        path_nodes = [target, neighbor]
        path_edges = [edge_map.get((target, neighbor))]
        paths.append(
            {
                "path_text": " -> ".join(path_nodes),
                "nodes": [node_map[node_id] for node_id in path_nodes if node_id in node_map],
                "edges": [edge for edge in path_edges if edge is not None],
            }
        )
        if len(paths) >= max_paths:
            break

    if len(paths) < max_paths:
        for neighbor in adjacency.get(target, set()):
            for second_hop in adjacency.get(neighbor, set()):
                if second_hop == target:
                    continue
                path_nodes = [target, neighbor, second_hop]
                path_edges = [
                    edge_map.get((target, neighbor)),
                    edge_map.get((neighbor, second_hop)),
                ]
                candidate = {
                    "path_text": " -> ".join(path_nodes),
                    "nodes": [
                        node_map[node_id] for node_id in path_nodes if node_id in node_map
                    ],
                    "edges": [edge for edge in path_edges if edge is not None],
                }
                if candidate not in paths:
                    paths.append(candidate)
                if len(paths) >= max_paths:
                    break
            if len(paths) >= max_paths:
                break

    return ToolResult(
        tool_name="graph_relation_trace",
        success=bool(paths),
        data={
            "core_entity": target,
            "graph": graph_dict,
            "paths": paths[:max_paths],
            "summary": f"Found {min(len(paths), max_paths)} candidate graph paths for '{target}'",
        },
        error=None if paths else f"No graph paths found for '{target}'",
        metadata={"core_entity": target, "path_count": min(len(paths), max_paths)},
    )
