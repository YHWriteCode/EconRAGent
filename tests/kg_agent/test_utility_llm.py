import logging

import pytest

from kg_agent.config import (
    AgentLLMClient,
    AgentModelConfig,
    FallbackLLMClient,
    KGAgentConfig,
)


class _StubLLMClient:
    def __init__(self, *, available: bool, text: str = "ok", payload=None):
        self._available = available
        self.text = text
        self.payload = payload or {"result": "ok"}
        self.calls: list[str] = []

    def is_available(self):
        return self._available

    async def complete_text(self, **kwargs):
        self.calls.append("complete_text")
        return self.text

    async def complete_json(self, **kwargs):
        self.calls.append("complete_json")
        return dict(self.payload)

    async def stream_text(self, **kwargs):
        self.calls.append("stream_text")
        yield self.text


def test_kg_agent_config_reads_utility_model_from_env(monkeypatch):
    monkeypatch.setenv("KG_AGENT_UTILITY_MODEL_NAME", "utility-mini")
    monkeypatch.setenv("KG_AGENT_UTILITY_MODEL_BASE_URL", "http://utility.local/v1")
    monkeypatch.setenv("KG_AGENT_UTILITY_MODEL_API_KEY", "utility-key")
    monkeypatch.setenv("KG_AGENT_UTILITY_MODEL_TIMEOUT_S", "12")

    config = KGAgentConfig.from_env()

    assert config.utility_model.model_name == "utility-mini"
    assert config.utility_model.base_url == "http://utility.local/v1"
    assert config.utility_model.api_key == "utility-key"
    assert config.utility_model.timeout_s == pytest.approx(12.0)


def test_agent_model_config_rejects_unsupported_provider():
    config = AgentModelConfig(
        provider="azure_openai",
        model_name="azure-model",
        base_url="https://example.com",
        api_key="key",
    )

    client = AgentLLMClient(config)

    assert client.is_available() is False


@pytest.mark.asyncio
async def test_fallback_llm_client_prefers_primary_without_warning(caplog):
    primary = _StubLLMClient(available=True, payload={"result": "primary"})
    fallback = _StubLLMClient(available=True, payload={"result": "fallback"})
    client = FallbackLLMClient(
        primary=primary,
        fallback=fallback,
        label="route planning",
    )

    caplog.set_level(logging.WARNING)
    payload = await client.complete_json(system_prompt="s", user_prompt="u")

    assert payload["result"] == "primary"
    assert primary.calls == ["complete_json"]
    assert fallback.calls == []
    assert "falling back" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_fallback_llm_client_warns_once_and_uses_fallback(caplog):
    primary = _StubLLMClient(available=False)
    fallback = _StubLLMClient(available=True, payload={"result": "fallback"})
    client = FallbackLLMClient(
        primary=primary,
        fallback=fallback,
        label="path explanation",
    )

    caplog.set_level(logging.WARNING)
    first = await client.complete_json(system_prompt="s", user_prompt="u")
    second = await client.complete_json(system_prompt="s", user_prompt="u")

    assert first["result"] == "fallback"
    assert second["result"] == "fallback"
    assert fallback.calls == ["complete_json", "complete_json"]
    assert caplog.text.lower().count("falling back to the main agent model") == 1
