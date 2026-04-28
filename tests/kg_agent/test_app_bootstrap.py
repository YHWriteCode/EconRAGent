import pytest
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path
import sys

from kg_agent.agent.agent_core import AgentCore
from kg_agent.api.app import (
    EnvLightRAGProvider,
    _build_domain_schema_addon_params,
    _normalize_ollama_host,
    _build_optional_utility_llm_from_env,
    build_mcp_adapter_from_env,
    build_rag_from_env,
    create_app,
)
from kg_agent.api.agent_routes import create_agent_routes
from kg_agent.config import (
    APIConfig,
    AgentModelConfig,
    AgentRuntimeConfig,
    KGAgentConfig,
    MCPConfig,
    MCPServerConfig,
    SchedulerConfig,
    SkillRuntimeConfig,
    ToolConfig,
)


class _FakeRAG:
    workspace = ""


def test_build_rag_from_env_returns_lightrag_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("KG_AGENT_DEFAULT_DOMAIN_SCHEMA", raising=False)
    monkeypatch.delenv("KG_AGENT_SUMMARY_LANGUAGE", raising=False)
    monkeypatch.delenv("SUMMARY_LANGUAGE", raising=False)
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
    assert rag.addon_params["domain_schema"]["profile_name"] == "economy"
    assert rag.addon_params["domain_schema"]["enabled"] is True
    assert rag.addon_params["language"] == "Chinese"
    assert "Company" in rag.addon_params["entity_types"]


def test_build_domain_schema_addon_params_supports_general_opt_out():
    economy_params = _build_domain_schema_addon_params(None)
    general_params = _build_domain_schema_addon_params("general")

    assert economy_params["domain_schema"]["profile_name"] == "economy"
    assert economy_params["domain_schema"]["enabled"] is True
    assert economy_params["language"] == "Chinese"
    assert economy_params["domain_schema"]["language"] == "Chinese"
    assert general_params["domain_schema"]["profile_name"] == "general"
    assert general_params["domain_schema"]["enabled"] is False
    assert general_params["language"] == "Chinese"


def test_build_rag_from_env_wires_rerank_settings(tmp_path, monkeypatch):
    sentinel = object()
    monkeypatch.delenv("KG_AGENT_DEFAULT_DOMAIN_SCHEMA", raising=False)
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
    monkeypatch.setattr(
        "kg_agent.api.app._build_rerank_model_func_from_env",
        lambda: (sentinel, 0.42),
    )

    rag = build_rag_from_env()

    assert rag.rerank_model_func is sentinel
    assert rag.min_rerank_score == pytest.approx(0.42)


def test_build_optional_utility_llm_from_env_returns_none_without_dedicated_config(
    monkeypatch,
):
    monkeypatch.delenv("UTILITY_LLM_MODEL", raising=False)
    monkeypatch.delenv("UTILITY_LLM_BINDING_HOST", raising=False)
    monkeypatch.delenv("KG_AGENT_UTILITY_MODEL_NAME", raising=False)
    monkeypatch.delenv("KG_AGENT_UTILITY_MODEL_BASE_URL", raising=False)

    llm_func, model_name = _build_optional_utility_llm_from_env()

    assert llm_func is None
    assert model_name is None


def test_build_rag_from_env_wires_utility_llm(tmp_path, monkeypatch):
    sentinel = object()
    monkeypatch.delenv("KG_AGENT_DEFAULT_DOMAIN_SCHEMA", raising=False)
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
    monkeypatch.setattr(
        "kg_agent.api.app._build_optional_utility_llm_from_env",
        lambda: (sentinel, "utility-mini"),
    )

    rag = build_rag_from_env()

    assert rag.utility_llm_model_func is sentinel
    assert rag.utility_llm_model_name == "utility-mini"


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
    assert any(route.path == "/agent/skills" for route in app.router.routes)
    assert any(route.path == "/agent/skill-runs/{run_id}" for route in app.router.routes)
    assert any(route.path == "/agent/skill-runs/{run_id}/cancel" for route in app.router.routes)
    assert any(route.path == "/agent/skill-runs/{run_id}/logs" for route in app.router.routes)


def test_build_mcp_adapter_from_env_returns_adapter(monkeypatch):
    monkeypatch.setenv(
        "KG_AGENT_MCP_SERVERS_JSON",
        '[{"name":"quant-skill","command":"python","args":["server.py"]}]',
    )
    monkeypatch.setenv(
        "KG_AGENT_MCP_CAPABILITIES_JSON",
        (
            '[{"name":"quant_backtest_skill","description":"Run a backtest.",'
            '"server":"quant-skill","input_schema":{"type":"object"}}]'
        ),
    )

    adapter = build_mcp_adapter_from_env()

    assert adapter is not None
    assert adapter.has_capabilities() is True


def test_build_mcp_adapter_from_env_returns_adapter_for_discovery_only(monkeypatch):
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    monkeypatch.setenv(
        "KG_AGENT_MCP_SERVERS_JSON",
        json.dumps(
            [
                {
                    "name": "quant-skill",
                    "command": sys.executable,
                    "args": [str(fixture_path)],
                    "discover_tools": True,
                }
            ]
        ),
    )
    monkeypatch.setenv("KG_AGENT_MCP_CAPABILITIES_JSON", "[]")

    adapter = build_mcp_adapter_from_env()

    assert adapter is not None
    assert adapter.discovery_enabled() is True
    assert adapter.has_capabilities() is True


def test_build_mcp_adapter_from_env_returns_adapter_for_runtime_only_server(monkeypatch):
    monkeypatch.setenv(
        "KG_AGENT_MCP_SERVERS_JSON",
        '[{"name":"skill-runtime","command":"docker","args":["run","--rm","skill-runtime"],"discover_tools":false}]',
    )
    monkeypatch.setenv("KG_AGENT_MCP_CAPABILITIES_JSON", "[]")

    adapter = build_mcp_adapter_from_env()

    assert adapter is not None
    assert adapter.discovery_enabled() is False
    assert adapter.has_capabilities() is False


def test_health_reports_default_workspace_and_active_workspace_count(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("KG_AGENT_MCP_SERVERS_JSON", raising=False)
    monkeypatch.delenv("KG_AGENT_MCP_CAPABILITIES_JSON", raising=False)
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
    monkeypatch.setenv("KG_AGENT_DEFAULT_WORKSPACE", "ops-default")

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_workspace"] == "ops-default"
    assert payload["active_workspace_count"] == 0
    assert payload["active_workspaces"] == []
    assert payload["dynamic_workspace_enabled"] is True
    assert payload["mcp_configured"] is False
    assert payload["mcp_capability_count"] == 0


def test_health_reports_discovered_mcp_capability_count():
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=MCPConfig(
            servers=[
                MCPServerConfig(
                    name="quant-skill",
                    command=sys.executable,
                    args=[str(fixture_path)],
                    discover_tools=True,
                    startup_timeout_s=5.0,
                    tool_timeout_s=5.0,
                )
            ]
        ),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )
    agent_core = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=build_mcp_adapter_from_env(config=config),
    )
    app = create_app(agent_core=agent_core, config=config)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mcp_configured"] is True
    assert payload["mcp_capability_count"] == 2


def test_agent_core_auto_wires_skill_runtime_client_with_runtime_only_mcp_server():
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False),
        mcp=MCPConfig(
            servers=[
                MCPServerConfig(
                    name="skill-runtime",
                    command="docker",
                    args=["run", "--rm", "fake-skill-runtime"],
                    discover_tools=False,
                )
            ],
            capabilities=[],
        ),
        skill_runtime=SkillRuntimeConfig(server="skill-runtime"),
        runtime=AgentRuntimeConfig(default_workspace="", max_iterations=1),
    )

    agent = AgentCore(
        rag=_FakeRAG(),
        config=config,
        mcp_adapter=build_mcp_adapter_from_env(config=config),
    )

    assert agent.mcp_adapter is not None
    assert agent.skill_executor.runtime_client is not None


def test_normalize_ollama_host_strips_openai_style_suffix():
    assert _normalize_ollama_host("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434"
    assert _normalize_ollama_host("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434"
    assert _normalize_ollama_host("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


@pytest.mark.asyncio
async def test_env_rag_provider_reuses_same_workspace_instance(monkeypatch):
    created_workspaces: list[str] = []
    created_profiles: list[str | None] = []
    monkeypatch.delenv("KG_AGENT_DEFAULT_DOMAIN_SCHEMA", raising=False)

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

    def fake_build_rag_from_env(*, workspace=None, domain_schema_profile=None):
        created_workspaces.append(workspace or "")
        created_profiles.append(domain_schema_profile)
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
    assert created_profiles == ["economy", "economy"]

    await provider.finalize_all()

    assert rag_a1.finalize_calls == 1
    assert rag_b.finalize_calls == 1


def test_agent_chat_stream_endpoint_returns_sse():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            raise AssertionError("non-stream path should not be used")

        async def chat_stream(self, **kwargs):
            yield {"type": "meta", "metadata": {"streaming_supported": True}}
            yield {"type": "route", "route": {"strategy": "factual_qa"}}
            yield {"type": "tool_start", "tool": "kg_hybrid_search", "index": 1}
            yield {
                "type": "tool_result",
                "tool_call": {"tool": "kg_hybrid_search", "success": True},
            }
            yield {"type": "answer_start"}
            yield {"type": "delta", "content": "hello"}
            yield {"type": "done", "answer": "hello"}

    app = FastAPI()
    app.include_router(create_agent_routes(_StubAgentCore()))

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/agent/chat",
            json={
                "query": "hello",
                "session_id": "stream-session",
                "stream": True,
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert 'event: route\ndata: {"type": "route", "route": {"strategy": "factual_qa"}}' in body
    assert 'data: {"type": "route", "route": {"strategy": "factual_qa"}}' in body
    assert 'data: {"type": "tool_start", "tool": "kg_hybrid_search", "index": 1}' in body
    assert 'data: {"type": "delta", "content": "hello"}' in body
    assert '"type": "done"' in body


def test_agent_chat_stream_endpoint_emits_structured_error_event():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            raise AssertionError("non-stream path should not be used")

        async def chat_stream(self, **kwargs):
            yield {"type": "meta", "metadata": {"streaming_supported": True}}
            raise RuntimeError("stream exploded")

    app = FastAPI()
    app.include_router(create_agent_routes(_StubAgentCore()))

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/agent/chat",
            json={
                "query": "hello",
                "session_id": "stream-session",
                "stream": True,
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert 'event: error\ndata: {"type": "error"' in body
    assert '"code": "stream_error"' in body


def test_create_app_enables_cors_for_configured_origin():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    app = create_app(
        agent_core=_StubAgentCore(),
        config=KGAgentConfig(
            api=APIConfig(cors_origins=["http://localhost:3000"]),
        ),
    )

    with TestClient(app) as client:
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_create_app_serves_webui_redirect_and_index():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app) as client:
        root_response = client.get("/", follow_redirects=False)
        webui_response = client.get("/webui")

    assert root_response.status_code == 307
    assert root_response.headers["location"] == "/webui/chat"
    assert webui_response.status_code == 200
    assert "text/html" in webui_response.headers["content-type"]


def test_validation_errors_use_uniform_error_envelope():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app) as client:
        response = client.post(
            "/agent/chat",
            json={"session_id": "missing-query"},
        )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["status_code"] == 422
    assert payload["error"]["details"]


def test_unhandled_errors_use_internal_error_envelope():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            raise RuntimeError("secret stack detail")

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/agent/chat",
            json={"query": "hello", "session_id": "err-session"},
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["message"] == "Internal server error."


def test_value_error_routes_use_bad_request_envelope():
    class _StubAgentCore:
        def read_skill_file(self, skill_name: str, relative_path: str):
            raise ValueError("relative_path must stay inside the skill directory")

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app) as client:
        response = client.get("/agent/skills/demo/files/reference.md")

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "bad_request"
    assert "relative_path must stay inside the skill directory" in payload["error"]["message"]


def test_runtime_error_routes_use_service_unavailable_envelope():
    class _StubAgentCore:
        async def get_skill_run_status(self, *, run_id: str):
            raise RuntimeError("Skill runtime client is not configured")

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app) as client:
        response = client.get("/agent/skill-runs/skill-run-123")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "service_unavailable"
    assert "Skill runtime client is not configured" in payload["error"]["message"]


def test_ready_reports_ready_when_agent_core_and_rag_provider_exist(
    tmp_path,
    monkeypatch,
):
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

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["agent_core"] is True
    assert payload["checks"]["rag"] is True


def test_ready_reports_not_ready_without_rag_binding():
    class _StubAgentCore:
        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    app = create_app(agent_core=_StubAgentCore())

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["agent_core"] is True
    assert payload["checks"]["rag"] is False


def test_create_app_starts_cross_session_background_maintenance():
    class _StubCrossSessionStore:
        def __init__(self):
            self.enable_background_maintenance = True
            self.maintenance_running = False
            self.start_calls = 0
            self.close_calls = 0

        async def start_background_maintenance(self):
            self.start_calls += 1
            self.maintenance_running = True
            return True

        async def close(self):
            self.close_calls += 1
            self.maintenance_running = False

    class _StubAgentCore:
        def __init__(self):
            self.cross_session_store = _StubCrossSessionStore()

        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    agent_core = _StubAgentCore()
    app = create_app(agent_core=agent_core)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cross_session_maintenance_enabled"] is True
    assert payload["cross_session_maintenance_running"] is True
    assert agent_core.cross_session_store.start_calls == 1
    assert agent_core.cross_session_store.close_calls == 1


def test_create_app_bootstraps_scheduler_sources_from_file(tmp_path):
    class _StubCrawlerAdapter:
        async def close(self):
            return None

    class _StubAgentCore:
        def __init__(self):
            self.crawler_adapter = _StubCrawlerAdapter()
            self.crawl_state_store = None
            self.utility_llm_client = None

        async def _resolve_rag(self, workspace=None):
            return _FakeRAG()

        async def chat(self, **kwargs):
            return None

        async def chat_stream(self, **kwargs):
            if False:
                yield None

    sources_file = tmp_path / "sources.json"
    state_file = tmp_path / "state.json"
    bootstrap_file = tmp_path / "bootstrap_sources.json"
    bootstrap_file.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "default-feed",
                        "name": "Default Feed",
                        "urls": ["https://example.com/feed.xml"],
                        "category": "economy",
                        "source_type": "auto",
                        "max_pages": 5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = KGAgentConfig(
        agent_model=AgentModelConfig(provider="disabled"),
        tool_config=ToolConfig(enable_memory=False, enable_web_search=True),
        scheduler=SchedulerConfig(
            enable_scheduler=False,
            sources_file=str(sources_file),
            state_file=str(state_file),
            bootstrap_sources_file=str(bootstrap_file),
        ),
        runtime=AgentRuntimeConfig(
            default_workspace="manual-default",
            network_ingest_workspace="kg_agent_network",
            max_iterations=1,
        ),
    )
    app = create_app(agent_core=_StubAgentCore(), config=config)

    with TestClient(app) as client:
        response = client.get("/agent/sources")

    assert response.status_code == 200
    payload = response.json()
    assert [item["source_id"] for item in payload["sources"]] == ["default-feed"]
    source = payload["sources"][0]
    assert source["workspace"] == "kg_agent_network"
    assert source["resolved_source_type"] == "feed"
    assert source["resolved_schedule_mode"] == "adaptive_feed"
