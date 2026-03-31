import pytest

from kg_agent.api.app import (
    EnvLightRAGProvider,
    _normalize_ollama_host,
    build_rag_from_env,
    create_app,
)


def test_build_rag_from_env_returns_lightrag_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKING_DIR", str(tmp_path / "rag_storage"))
    monkeypatch.setenv("LIGHTRAG_KV_STORAGE", "JsonKVStorage")
    monkeypatch.setenv("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage")
    monkeypatch.setenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")
    monkeypatch.setenv("LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage")
    monkeypatch.setenv("LLM_BINDING", "openai")
    monkeypatch.setenv("EMBEDDING_BINDING", "openai")
    monkeypatch.setenv("LLM_BINDING_HOST", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("EMBEDDING_BINDING_HOST", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("LLM_BINDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_BINDING_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "dummy-chat-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "dummy-embed-model")
    monkeypatch.setenv("EMBEDDING_DIM", "1536")

    rag = build_rag_from_env()

    assert rag.llm_model_name == "dummy-chat-model"
    assert rag.embedding_func.embedding_dim == 1536
    assert rag.vector_storage == "NanoVectorDBStorage"


def test_create_app_bootstraps_rag_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKING_DIR", str(tmp_path / "rag_storage"))
    monkeypatch.setenv("LIGHTRAG_KV_STORAGE", "JsonKVStorage")
    monkeypatch.setenv("LIGHTRAG_GRAPH_STORAGE", "NetworkXStorage")
    monkeypatch.setenv("LIGHTRAG_VECTOR_STORAGE", "NanoVectorDBStorage")
    monkeypatch.setenv("LIGHTRAG_DOC_STATUS_STORAGE", "JsonDocStatusStorage")
    monkeypatch.setenv("LLM_BINDING", "openai")
    monkeypatch.setenv("EMBEDDING_BINDING", "openai")
    monkeypatch.setenv("LLM_BINDING_HOST", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("EMBEDDING_BINDING_HOST", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("LLM_BINDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_BINDING_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "dummy-chat-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "dummy-embed-model")
    monkeypatch.setenv("EMBEDDING_DIM", "1536")

    app = create_app()

    assert app.state.rag is None
    assert app.state.rag_provider is not None
    assert app.state.agent_core is not None
    assert any(route.path == "/agent/chat" for route in app.router.routes)


def test_normalize_ollama_host_strips_openai_style_suffix():
    assert _normalize_ollama_host("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert _normalize_ollama_host("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434"
    assert _normalize_ollama_host("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


@pytest.mark.asyncio
async def test_env_rag_provider_reuses_same_workspace_instance(monkeypatch):
    created_workspaces: list[str] = []

    class DummyRAG:
        def __init__(self, workspace: str):
            self.workspace = workspace
            self.initialize_calls = 0
            self.migrate_calls = 0
            self.finalize_calls = 0

        async def initialize_storages(self):
            self.initialize_calls += 1

        async def check_and_migrate_data(self):
            self.migrate_calls += 1

        async def finalize_storages(self):
            self.finalize_calls += 1

    def fake_build_rag_from_env(*, workspace=None):
        created_workspaces.append(workspace or "")
        return DummyRAG(workspace or "")

    monkeypatch.setattr("kg_agent.api.app.build_rag_from_env", fake_build_rag_from_env)
    monkeypatch.setattr("kg_agent.api.app.finalize_share_data", lambda: None)

    provider = EnvLightRAGProvider()

    rag_a1 = await provider.get("ws-a")
    rag_a2 = await provider.get("ws-a")
    rag_b = await provider.get("ws-b")

    assert rag_a1 is rag_a2
    assert rag_a1 is not rag_b
    assert created_workspaces == ["ws-a", "ws-b"]
    assert rag_a1.initialize_calls == 1
    assert rag_a1.migrate_calls == 1
    assert rag_b.initialize_calls == 1

    await provider.finalize_all()

    assert rag_a1.finalize_calls == 1
    assert rag_b.finalize_calls == 1
