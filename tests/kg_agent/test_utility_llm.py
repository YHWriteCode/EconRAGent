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


class _StubOpenAIResponseMessage:
    def __init__(self, content: str):
        self.content = content


class _StubOpenAIChoice:
    def __init__(self, content: str):
        self.message = _StubOpenAIResponseMessage(content)


class _StubOpenAIResponse:
    def __init__(self, content: str):
        self.choices = [_StubOpenAIChoice(content)]


class _StubChatCompletions:
    def __init__(self, responses: list[str], *, fail_json_mode: bool = False):
        self.responses = list(responses)
        self.fail_json_mode = fail_json_mode
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("response_format") and self.fail_json_mode:
            raise RuntimeError("json mode unsupported")
        if not self.responses:
            raise RuntimeError("No stub response remaining")
        return _StubOpenAIResponse(self.responses.pop(0))


class _StubOpenAIClient:
    def __init__(self, responses: list[str], *, fail_json_mode: bool = False):
        self.chat = type(
            "_ChatNamespace",
            (),
            {"completions": _StubChatCompletions(responses, fail_json_mode=fail_json_mode)},
        )()


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


@pytest.mark.asyncio
async def test_agent_llm_client_complete_json_accepts_python_literal_style_objects(monkeypatch):
    client = AgentLLMClient(
        AgentModelConfig(
            provider="openai_compatible",
            model_name="stub-model",
            base_url="http://stub.local/v1",
            api_key="stub-key",
        )
    )

    async def _fake_chat_completion_text(**kwargs):
        return "{'mode': 'manual_required', 'flag': True, 'value': None}"

    monkeypatch.setattr(client, "_chat_completion_text", _fake_chat_completion_text)

    payload = await client.complete_json(system_prompt="s", user_prompt="u")

    assert payload == {"mode": "manual_required", "flag": True, "value": None}


@pytest.mark.asyncio
async def test_agent_llm_client_complete_json_repairs_malformed_json_with_second_call(monkeypatch):
    client = AgentLLMClient(
        AgentModelConfig(
            provider="openai_compatible",
            model_name="stub-model",
            base_url="http://stub.local/v1",
            api_key="stub-key",
        )
    )
    calls: list[dict] = []
    responses = iter(
        [
            '{"mode": "manual_required", "rationale": "bad "quote" here"}',
            '{"mode": "manual_required", "rationale": "bad \\"quote\\" here"}',
        ]
    )

    async def _fake_chat_completion_text(**kwargs):
        calls.append(dict(kwargs))
        return next(responses)

    monkeypatch.setattr(client, "_chat_completion_text", _fake_chat_completion_text)

    payload = await client.complete_json(system_prompt="s", user_prompt="u")

    assert payload == {
        "mode": "manual_required",
        "rationale": 'bad "quote" here',
    }
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_agent_llm_client_complete_json_prefers_json_mode_when_available(monkeypatch):
    client = AgentLLMClient(
        AgentModelConfig(
            provider="openai_compatible",
            model_name="stub-model",
            base_url="http://stub.local/v1",
            api_key="stub-key",
        )
    )
    stub_client = _StubOpenAIClient(['{"mode":"free_shell","command":"python run.py"}'])
    monkeypatch.setattr(client, "_ensure_client", lambda: stub_client)

    payload = await client.complete_json(system_prompt="s", user_prompt="u")

    assert payload == {"mode": "free_shell", "command": "python run.py"}
    assert stub_client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_agent_llm_client_complete_json_falls_back_when_json_mode_is_unsupported(monkeypatch):
    client = AgentLLMClient(
        AgentModelConfig(
            provider="openai_compatible",
            model_name="stub-model",
            base_url="http://stub.local/v1",
            api_key="stub-key",
        )
    )
    stub_client = _StubOpenAIClient(
        ['{"mode":"manual_required","failure_reason":"llm_planning_failed"}'],
        fail_json_mode=True,
    )
    monkeypatch.setattr(client, "_ensure_client", lambda: stub_client)

    payload = await client.complete_json(system_prompt="s", user_prompt="u")

    assert payload == {"mode": "manual_required", "failure_reason": "llm_planning_failed"}
    assert len(stub_client.chat.completions.calls) == 2
    assert "response_format" not in stub_client.chat.completions.calls[1]
