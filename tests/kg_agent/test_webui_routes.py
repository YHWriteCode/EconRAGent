import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kg_agent.agent.path_explainer import PathExplanation
from kg_agent.api.webui_routes import create_webui_routes
from kg_agent.crawler.crawl_state_store import CrawlStateRecord, EventClusterRecord
from kg_agent.memory.conversation_memory import ConversationMemoryStore
from kg_agent.uploads import UploadStore
from kg_agent.workspace_registry import InMemoryWorkspaceRegistry, WorkspaceRecord


@dataclass
class _FakeDocStatus:
    status: str
    updated_at: str
    content_summary: str = ""


class _FakeDocStatusStore:
    def __init__(
        self,
        *,
        status_counts: dict[str, int],
        latest_docs: list[tuple[str, _FakeDocStatus]],
        by_id: dict[str, dict | _FakeDocStatus] | None = None,
    ):
        self.status_counts = dict(status_counts)
        self.latest_docs = list(latest_docs)
        self.by_id = dict(by_id or {})
        self.by_track: dict[str, dict[str, _FakeDocStatus]] = {}

    async def get_all_status_counts(self) -> dict[str, int]:
        return dict(self.status_counts)

    async def get_docs_paginated(self, **kwargs):
        return list(self.latest_docs), len(self.latest_docs)

    async def get_by_id(self, doc_id: str):
        return self.by_id.get(doc_id)


class _FakeNode:
    def __init__(self, *, node_id: str, entity_type: str, updated_at: str):
        self.node_id = node_id
        self.entity_type = entity_type
        self.updated_at = updated_at

    def model_dump(self):
        return {
            "id": self.node_id,
            "labels": [self.entity_type],
            "properties": {
                "entity_type": self.entity_type,
                "updated_at": self.updated_at,
            },
        }


class _FakeEdge:
    def __init__(
        self,
        *,
        edge_id: str,
        source: str,
        target: str,
        relation_type: str,
        updated_at: str,
    ):
        self.edge_id = edge_id
        self.source = source
        self.target = target
        self.relation_type = relation_type
        self.updated_at = updated_at

    def model_dump(self):
        return {
            "id": self.edge_id,
            "type": self.relation_type,
            "source": self.source,
            "target": self.target,
            "properties": {
                "updated_at": self.updated_at,
                "relation_type": self.relation_type,
            },
        }


class _FakeChunkGraph:
    def __init__(self, workspace_id: str):
        base_time = "2026-04-04T08:00:00+00:00"
        if workspace_id == "ws-alpha":
            self.nodes = {
                "Acme": {
                    "entity_name": "Acme",
                    "entity_type": "Company",
                    "updated_at": base_time,
                },
                "Macro": {
                    "entity_name": "Macro",
                    "entity_type": "Metric",
                    "updated_at": "2026-04-03T08:00:00+00:00",
                },
                "Policy": {
                    "entity_name": "Policy",
                    "entity_type": "Policy",
                    "updated_at": "2026-03-01T08:00:00+00:00",
                },
            }
            self.edges = {
                ("Acme", "Macro"): {
                    "relation": "affected_by",
                    "updated_at": base_time,
                },
                ("Macro", "Policy"): {
                    "relation": "linked_to",
                    "updated_at": "2026-04-03T08:00:00+00:00",
                },
            }
        else:
            self.nodes = {
                "BetaCorp": {
                    "entity_name": "BetaCorp",
                    "entity_type": "Company",
                    "updated_at": "2026-04-02T08:00:00+00:00",
                },
                "Rates": {
                    "entity_name": "Rates",
                    "entity_type": "Metric",
                    "updated_at": "2026-04-02T08:00:00+00:00",
                },
            }
            self.edges = {
                ("BetaCorp", "Rates"): {
                    "relation": "sensitive_to",
                    "updated_at": "2026-04-02T08:00:00+00:00",
                },
            }

    async def has_node(self, node_id: str) -> bool:
        return node_id in self.nodes

    async def search_labels(self, query: str, limit: int = 50) -> list[str]:
        normalized = (query or "").strip().lower()
        labels = [
            node_id
            for node_id in self.nodes
            if not normalized or normalized in node_id.lower()
        ]
        return labels[:limit]

    async def get_node(self, node_id: str):
        return self.nodes.get(node_id)

    async def get_node_edges(self, node_id: str):
        return [
            (source, target)
            for source, target in self.edges
            if source == node_id or target == node_id
        ]

    async def get_edge(self, source: str, target: str):
        return self.edges.get((source, target)) or self.edges.get((target, source))


class _FakeRAG:
    def __init__(self, workspace_id: str):
        self.workspace = workspace_id
        self.chunk_entity_relation_graph = _FakeChunkGraph(workspace_id)
        if workspace_id == "ws-alpha":
            self.doc_status = _FakeDocStatusStore(
                status_counts={"processed": 2, "failed": 1},
                latest_docs=[
                    (
                        "doc-alpha-1",
                        _FakeDocStatus(
                            status="processed",
                            updated_at="2026-04-05T10:00:00+00:00",
                            content_summary="Alpha workspace latest summary",
                        ),
                    )
                ],
                by_id={
                    "doc-alpha-news": {
                        "content_summary": "由文档状态回退生成的事件摘要",
                    }
                },
            )
        else:
            self.doc_status = _FakeDocStatusStore(
                status_counts={"processed": 1},
                latest_docs=[
                    (
                        "doc-beta-1",
                        _FakeDocStatus(
                            status="processed",
                            updated_at="2026-04-02T09:00:00+00:00",
                            content_summary="Beta workspace summary",
                        ),
                    )
                ],
            )
        self.insert_calls: list[tuple[object, object]] = []
        self._track_counter = 0

    async def get_graph_labels(self):
        return list(self.chunk_entity_relation_graph.nodes)

    async def get_knowledge_graph(
        self,
        *,
        node_label: str,
        max_depth: int,
        max_nodes: int,
    ):
        del max_depth
        nodes = [
            _FakeNode(
                node_id=node_id,
                entity_type=str(payload.get("entity_type") or "unknown"),
                updated_at=str(payload.get("updated_at") or "2026-04-01T00:00:00+00:00"),
            )
            for node_id, payload in self.chunk_entity_relation_graph.nodes.items()
        ]
        edges = [
            _FakeEdge(
                edge_id=f"{source}->{target}",
                source=source,
                target=target,
                relation_type=str(payload.get("relation") or "related_to"),
                updated_at=str(payload.get("updated_at") or "2026-04-01T00:00:00+00:00"),
            )
            for (source, target), payload in self.chunk_entity_relation_graph.edges.items()
        ]
        if node_label and node_label != "*":
            allowed = {node_label}
            allowed.update(
                target if source == node_label else source
                for source, target in self.chunk_entity_relation_graph.edges
                if source == node_label or target == node_label
            )
            nodes = [node for node in nodes if node.node_id in allowed]
            edges = [
                edge
                for edge in edges
                if edge.source in allowed and edge.target in allowed
            ]
        is_truncated = len(nodes) > max_nodes
        nodes = nodes[:max_nodes]
        allowed_node_ids = {node.node_id for node in nodes}
        edges = [
            edge
            for edge in edges
            if edge.source in allowed_node_ids and edge.target in allowed_node_ids
        ]
        return SimpleNamespace(nodes=nodes, edges=edges, is_truncated=is_truncated)

    async def ainsert(self, *, input, file_paths):
        self._track_counter += 1
        track_id = f"{self.workspace}-track-{self._track_counter}"
        self.insert_calls.append((input, file_paths))
        self.doc_status.by_track[track_id] = {
            f"{track_id}-doc": _FakeDocStatus(
                status="processed",
                updated_at="2026-04-06T09:00:00+00:00",
                content_summary=f"Imported from {file_paths}",
            )
        }
        return track_id

    async def aget_docs_by_track_id(self, track_id: str):
        return dict(self.doc_status.by_track.get(track_id, {}))


class _FakePathExplainer:
    async def explain(self, **kwargs):
        return PathExplanation(
            enabled=True,
            question_type="relation_explanation",
            core_entities=["Acme", "Macro"],
            paths=[],
            final_explanation="Acme 与 Macro 之间存在可解释的关系路径。",
            uncertainty=None,
        )


class _FakeCrawlerAdapter:
    async def crawl_url(self, url: str):
        return SimpleNamespace(
            success=True,
            markdown=f"Fetched content from {url}",
            final_url=url,
            error=None,
        )


class _FakeAgentCore:
    def __init__(self, *, conversation_memory: ConversationMemoryStore):
        self.conversation_memory = conversation_memory
        self.path_explainer = _FakePathExplainer()
        self.crawler_adapter = _FakeCrawlerAdapter()
        self.config = SimpleNamespace(
            runtime=SimpleNamespace(default_domain_schema="economy")
        )
        self._rag_by_workspace: dict[str, _FakeRAG] = {}

    async def _resolve_rag(self, workspace: str | None):
        workspace_id = (workspace or "").strip()
        if workspace_id not in self._rag_by_workspace:
            self._rag_by_workspace[workspace_id] = _FakeRAG(workspace_id)
        return self._rag_by_workspace[workspace_id]


@dataclass
class _FakeSource:
    source_id: str
    name: str
    urls: list[str]
    category: str
    workspace: str | None = None


class _FakeSourceRegistry:
    def __init__(self, sources: list[_FakeSource]):
        self._sources = {source.source_id: source for source in sources}

    async def get_source(self, source_id: str):
        return self._sources.get(source_id)


class _FakeStateStore:
    def __init__(self, records: list[CrawlStateRecord]):
        self._records = {record.source_id: record for record in records}

    async def get_record(self, source_id: str):
        return self._records.get(source_id)


class _FakeScheduler:
    def __init__(self, *, sources: list[_FakeSource], records: list[CrawlStateRecord]):
        self.sources = list(sources)
        self.state_store = _FakeStateStore(records)
        self.source_registry = _FakeSourceRegistry(sources)
        self.removed_source_ids: list[str] = []

    async def list_sources(self):
        return list(self.sources)

    async def remove_source(self, source_id: str):
        self.removed_source_ids.append(source_id)
        self.sources = [source for source in self.sources if source.source_id != source_id]
        return True


class _FakeRagProvider:
    def __init__(self):
        self.dropped_workspaces: list[str] = []

    async def drop_workspace(self, workspace: str | None):
        self.dropped_workspaces.append((workspace or "").strip())


def _seed_workspace_registry(registry: InMemoryWorkspaceRegistry) -> None:
    asyncio.run(
        registry.upsert_workspace(
            WorkspaceRecord(
                workspace_id="ws-alpha",
                display_name="Alpha Space",
                description="Alpha workspace",
                created_at="2026-04-01T00:00:00+00:00",
                updated_at="2026-04-02T00:00:00+00:00",
            )
        )
    )
    asyncio.run(
        registry.upsert_workspace(
            WorkspaceRecord(
                workspace_id="ws-beta",
                display_name="Beta Space",
                description="Beta workspace",
                created_at="2026-04-01T01:00:00+00:00",
                updated_at="2026-04-02T01:00:00+00:00",
            )
        )
    )


def _seed_conversation_memory(memory: ConversationMemoryStore) -> None:
    asyncio.run(
        memory.append_message(
            "session-alpha",
            "user",
            "跟进 Alpha 空间中的供应链异常",
            metadata={"workspace": "ws-alpha"},
            user_id="user-1",
        )
    )
    asyncio.run(
        memory.append_message(
            "session-alpha",
            "assistant",
            "已记录 Alpha 空间中的异常。",
            metadata={"workspace": "ws-alpha"},
            user_id="user-1",
        )
    )
    asyncio.run(
        memory.append_message(
            "session-beta",
            "user",
            "研究 Beta 空间中的利率冲击",
            metadata={"workspace": "ws-beta"},
            user_id="user-1",
        )
    )


def _build_scheduler() -> _FakeScheduler:
    sources = [
        _FakeSource(
            source_id="source-alpha",
            name="Alpha Feed",
            urls=["https://alpha.example/feed"],
            category="macro",
            workspace="ws-alpha",
        ),
        _FakeSource(
            source_id="source-beta",
            name="Beta Feed",
            urls=["https://beta.example/feed"],
            category="equity",
            workspace="ws-beta",
        ),
    ]
    records = [
        CrawlStateRecord(
            source_id="source-alpha",
            last_crawled_at="2026-04-05T12:00:00+00:00",
            event_clusters={
                "cluster-new": EventClusterRecord(
                    cluster_id="cluster-new",
                    headline="Alpha 供应链风险再次升温",
                    summary="",
                    representative_item_key="https://alpha.example/articles/alpha-risk",
                    member_item_keys=[
                        "https://alpha.example/articles/alpha-risk",
                        "https://mirror.example/alpha-risk",
                        "https://mirror.example/alpha-risk",
                    ],
                    active_doc_id="doc-alpha-news",
                    published_at="2026-04-05T09:00:00+00:00",
                    updated_at="2026-04-05T10:00:00+00:00",
                ),
                "cluster-old": EventClusterRecord(
                    cluster_id="cluster-old",
                    headline="Alpha 旧事件",
                    summary="已有聚合摘要",
                    representative_item_key="https://alpha.example/articles/old-event",
                    member_item_keys=["https://alpha.example/articles/old-event"],
                    active_doc_id="doc-alpha-old",
                    published_at="2026-04-01T09:00:00+00:00",
                    updated_at="2026-04-01T10:00:00+00:00",
                ),
            },
        ),
        CrawlStateRecord(
            source_id="source-beta",
            last_crawled_at="2026-04-04T12:00:00+00:00",
            event_clusters={
                "cluster-beta": EventClusterRecord(
                    cluster_id="cluster-beta",
                    headline="Beta 利率敏感性上升",
                    summary="Beta 的利率敏感性出现抬升。",
                    representative_item_key="https://beta.example/articles/beta-rates",
                    member_item_keys=["https://beta.example/articles/beta-rates"],
                    active_doc_id="doc-beta-news",
                    published_at="2026-04-04T09:00:00+00:00",
                    updated_at="2026-04-04T09:30:00+00:00",
                )
            },
        ),
    ]
    return _FakeScheduler(sources=sources, records=records)


def _build_client(tmp_path):
    memory = ConversationMemoryStore()
    _seed_conversation_memory(memory)
    agent_core = _FakeAgentCore(conversation_memory=memory)
    registry = InMemoryWorkspaceRegistry()
    _seed_workspace_registry(registry)
    upload_store = UploadStore(str(tmp_path / "uploads"))
    scheduler = _build_scheduler()
    rag_provider = _FakeRagProvider()

    app = FastAPI()
    app.include_router(
        create_webui_routes(
            agent_core,
            scheduler=scheduler,
            workspace_registry=registry,
            upload_store=upload_store,
            rag_provider=rag_provider,
        )
    )
    return TestClient(app), agent_core, scheduler, registry, upload_store, rag_provider


def test_webui_upload_session_and_workspace_routes(tmp_path):
    client, agent_core, scheduler, registry, upload_store, rag_provider = _build_client(
        tmp_path
    )

    upload_response = client.post(
        "/agent/uploads",
        files={"file": ("brief.md", b"# Brief\nAlpha workspace note", "text/markdown")},
    )
    assert upload_response.status_code == 200
    upload_payload = upload_response.json()
    upload_id = upload_payload["upload_id"]

    get_upload_response = client.get(f"/agent/uploads/{upload_id}")
    assert get_upload_response.status_code == 200
    assert get_upload_response.json()["filename"] == "brief.md"

    session_response = client.get("/agent/sessions", params={"workspace": "ws-alpha"})
    assert session_response.status_code == 200
    assert session_response.json()["sessions"][0]["session_id"] == "session-alpha"
    assert session_response.json()["sessions"][0]["title"] == "跟进 Alpha 空间中的供应链异常"

    messages_response = client.get("/agent/sessions/session-alpha/messages")
    assert messages_response.status_code == 200
    assert len(messages_response.json()["messages"]) == 2

    delete_response = client.delete("/agent/sessions/session-alpha")
    assert delete_response.status_code == 200
    empty_session_response = client.get("/agent/sessions", params={"workspace": "ws-alpha"})
    assert empty_session_response.status_code == 200
    assert empty_session_response.json()["sessions"] == []

    workspaces_response = client.get("/agent/workspaces")
    assert workspaces_response.status_code == 200
    alpha_workspace = next(
        item
        for item in workspaces_response.json()["workspaces"]
        if item["workspace_id"] == "ws-alpha"
    )
    assert alpha_workspace["document_count"] == 3
    assert alpha_workspace["node_count"] == 3
    assert alpha_workspace["source_count"] == 1
    assert alpha_workspace["last_updated_at"] == "2026-04-05T12:00:00+00:00"


def test_webui_workspace_crud_imports_and_delete(tmp_path):
    client, agent_core, scheduler, registry, upload_store, rag_provider = _build_client(
        tmp_path
    )

    create_response = client.post(
        "/agent/workspaces",
        json={"display_name": "New Space", "description": "Created from UI"},
    )
    assert create_response.status_code == 200
    created_workspace = create_response.json()
    created_workspace_id = created_workspace["workspace_id"]
    assert created_workspace["display_name"] == "New Space"
    assert created_workspace_id.startswith("new-space-")

    rename_response = client.patch(
        f"/agent/workspaces/{created_workspace_id}",
        json={"display_name": "Renamed Space", "description": "Updated"},
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["display_name"] == "Renamed Space"

    text_import_response = client.post(
        "/agent/workspaces/ws-alpha/imports",
        json={"kind": "text", "text": "Alpha imported text"},
    )
    assert text_import_response.status_code == 200
    text_track_id = text_import_response.json()["track_id"]

    url_import_response = client.post(
        "/agent/workspaces/ws-alpha/imports",
        json={"kind": "url", "url": "https://alpha.example/post/1"},
    )
    assert url_import_response.status_code == 200
    url_track_id = url_import_response.json()["track_id"]

    upload_response = client.post(
        "/agent/uploads",
        files={"file": ("import.txt", b"file import body", "text/plain")},
    )
    upload_id = upload_response.json()["upload_id"]
    upload_import_response = client.post(
        "/agent/workspaces/ws-alpha/imports",
        json={"kind": "upload", "upload_id": upload_id},
    )
    assert upload_import_response.status_code == 200
    upload_track_id = upload_import_response.json()["track_id"]

    for track_id in (text_track_id, url_track_id, upload_track_id):
        import_status_response = client.get(
            f"/agent/imports/{track_id}",
            params={"workspace": "ws-alpha"},
        )
        assert import_status_response.status_code == 200
        payload = import_status_response.json()
        assert payload["workspace_id"] == "ws-alpha"
        assert payload["document_count"] == 1
        assert payload["status_counts"] == {"processed": 1}

    delete_response = client.delete("/agent/workspaces/ws-beta")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "deleted", "workspace_id": "ws-beta"}
    assert rag_provider.dropped_workspaces == ["ws-beta"]
    assert scheduler.removed_source_ids == ["source-beta"]


def test_webui_graph_routes_expose_overview_filters_and_path_explanation(tmp_path):
    client, *_ = _build_client(tmp_path)

    overview_response = client.get("/agent/graph/overview", params={"workspace": "all", "max_nodes": 8})
    assert overview_response.status_code == 200
    overview_payload = overview_response.json()
    assert overview_payload["workspace"] == "all"
    assert {item["workspace_id"] for item in overview_payload["nodes"]} == {
        "ws-alpha",
        "ws-beta",
    }

    subgraph_response = client.get(
        "/agent/graph/subgraph",
        params={
            "workspace": "ws-alpha",
            "entity_type": "company",
            "time_from": "2026-04-01T00:00:00+00:00",
        },
    )
    assert subgraph_response.status_code == 200
    subgraph_payload = subgraph_response.json()
    assert subgraph_payload["summary"]["node_count"] == 1
    assert subgraph_payload["nodes"][0]["id"] == "Acme"

    relation_filtered_response = client.get(
        "/agent/graph/subgraph",
        params={
            "workspace": "ws-alpha",
            "relation_type": "affected_by",
        },
    )
    assert relation_filtered_response.status_code == 200
    relation_filtered_payload = relation_filtered_response.json()
    assert relation_filtered_payload["summary"]["edge_count"] == 1
    assert relation_filtered_payload["edges"][0]["type"] == "affected_by"
    assert {item["id"] for item in relation_filtered_payload["nodes"]} == {
        "Acme",
        "Macro",
    }

    schema_response = client.get("/agent/graph/schema")
    assert schema_response.status_code == 200
    schema_payload = schema_response.json()
    assert schema_payload["profile_name"] == "economy"
    assert {item["name"] for item in schema_payload["entity_types"]} >= {
        "Company",
        "Metric",
    }
    assert {
        item["name"] for item in schema_payload["relation_types"]
    } >= {"affects_metric", "belongs_to_industry"}

    labels_response = client.get(
        "/agent/graph/labels",
        params={"workspace": "all", "q": "a", "limit": 10},
    )
    assert labels_response.status_code == 200
    label_items = labels_response.json()["items"]
    assert {"workspace_id": "ws-alpha", "label": "Acme"} in label_items
    assert {"workspace_id": "ws-beta", "label": "BetaCorp"} in label_items

    entity_response = client.get(
        "/agent/graph/entities/Acme",
        params={"workspace": "ws-alpha"},
    )
    assert entity_response.status_code == 200
    assert entity_response.json()["neighbors"][0]["entity_id"] == "Macro"

    relation_response = client.get(
        "/agent/graph/relations",
        params={"workspace": "ws-alpha", "source": "Acme", "target": "Macro"},
    )
    assert relation_response.status_code == 200
    assert relation_response.json()["edge"]["relation"] == "affected_by"

    path_response = client.post(
        "/agent/graph/path_explain",
        json={"workspace": "ws-alpha", "source": "Acme", "target": "Macro"},
    )
    assert path_response.status_code == 200
    path_payload = path_response.json()
    assert path_payload["paths"][0]["path_text"] == "Acme -> Macro"
    assert (
        path_payload["path_explanation"]["final_explanation"]
        == "Acme 与 Macro 之间存在可解释的关系路径。"
    )


def test_webui_graph_routes_reallocate_unused_all_workspace_budget(tmp_path):
    class _ImbalancedFakeRAG:
        def __init__(self, workspace_id: str):
            self.workspace = workspace_id

        async def get_knowledge_graph(
            self,
            *,
            node_label: str,
            max_depth: int,
            max_nodes: int,
        ):
            del node_label, max_depth
            total_nodes = 5 if self.workspace == "ws-alpha" else 1
            nodes = [
                _FakeNode(
                    node_id=f"{self.workspace}-node-{index}",
                    entity_type="Company",
                    updated_at="2026-04-04T08:00:00+00:00",
                )
                for index in range(total_nodes)
            ][:max_nodes]
            return SimpleNamespace(
                nodes=nodes,
                edges=[],
                is_truncated=total_nodes > max_nodes,
            )

    class _ImbalancedAgentCore(_FakeAgentCore):
        async def _resolve_rag(self, workspace: str | None):
            workspace_id = (workspace or "").strip()
            if workspace_id not in self._rag_by_workspace:
                self._rag_by_workspace[workspace_id] = _ImbalancedFakeRAG(workspace_id)
            return self._rag_by_workspace[workspace_id]

    memory = ConversationMemoryStore()
    registry = InMemoryWorkspaceRegistry()
    _seed_workspace_registry(registry)
    upload_store = UploadStore(str(tmp_path / "uploads"))
    app = FastAPI()
    app.include_router(
        create_webui_routes(
            _ImbalancedAgentCore(conversation_memory=memory),
            scheduler=None,
            workspace_registry=registry,
            upload_store=upload_store,
            rag_provider=None,
        )
    )
    client = TestClient(app)

    response = client.get(
        "/agent/graph/overview",
        params={"workspace": "all", "max_nodes": 4},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["summary"]["node_count"] == 4
    workspace_counts: dict[str, int] = {}
    for node in payload["nodes"]:
        workspace_id = str(node["workspace_id"])
        workspace_counts[workspace_id] = workspace_counts.get(workspace_id, 0) + 1
    assert workspace_counts == {"ws-alpha": 3, "ws-beta": 1}
    assert payload["is_truncated"] is True


def test_webui_discover_routes_paginate_and_fallback_to_doc_summary(tmp_path):
    client, *_ = _build_client(tmp_path)

    first_page_response = client.get(
        "/agent/discover/events",
        params={"workspace": "ws-alpha", "limit": 1},
    )
    assert first_page_response.status_code == 200
    first_page_payload = first_page_response.json()
    assert len(first_page_payload["items"]) == 1
    first_event = first_page_payload["items"][0]
    assert first_event["event_id"] == "source-alpha:cluster-new"
    assert first_event["summary"] == "由文档状态回退生成的事件摘要"
    assert first_event["source_count"] == 2
    assert first_page_payload["next_cursor"]

    second_page_response = client.get(
        "/agent/discover/events",
        params={
            "workspace": "ws-alpha",
            "limit": 1,
            "cursor": first_page_payload["next_cursor"],
        },
    )
    assert second_page_response.status_code == 200
    assert second_page_response.json()["items"][0]["event_id"] == "source-alpha:cluster-old"

    detail_response = client.get("/agent/discover/events/source-alpha:cluster-new")
    assert detail_response.status_code == 200
    assert detail_response.json()["headline"] == "Alpha 供应链风险再次升温"

    sources_response = client.get(
        "/agent/discover/sources",
        params={"workspace": "ws-alpha"},
    )
    assert sources_response.status_code == 200
    assert sources_response.json()["sources"] == [
        {
            "source_id": "source-alpha",
            "name": "Alpha Feed",
            "workspace": "ws-alpha",
            "category": "macro",
            "urls": ["https://alpha.example/feed"],
        }
    ]
