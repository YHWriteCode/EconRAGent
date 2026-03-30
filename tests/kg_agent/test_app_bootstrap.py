from kg_agent.api.app import build_rag_from_env, create_app


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

    assert app.state.rag is not None
    assert app.state.agent_core is not None
    assert any(route.path == "/agent/chat" for route in app.router.routes)
