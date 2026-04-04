import pytest

import lightrag_fork.operate as operate_module
from lightrag_fork.operate import _summarize_descriptions


class _TinyTokenizer:
    def encode(self, text: str):
        return [token for token in str(text).split() if token]


async def _main_llm(prompt, **kwargs):
    return "main-summary"


async def _utility_llm(prompt, **kwargs):
    return "utility-summary"


def _global_config(*, utility_llm_model_func=None):
    return {
        "llm_model_func": _main_llm,
        "utility_llm_model_func": utility_llm_model_func,
        "workspace": "test-workspace",
        "addon_params": {"language": "English"},
        "summary_length_recommended": 60,
        "tokenizer": _TinyTokenizer(),
        "summary_context_size": 200,
    }


@pytest.mark.asyncio
async def test_summarize_descriptions_prefers_utility_llm():
    summary = await _summarize_descriptions(
        "entity",
        "BYD",
        ["electric vehicle maker", "battery supplier"],
        _global_config(utility_llm_model_func=_utility_llm),
        llm_response_cache=None,
    )

    assert summary == "utility-summary"


@pytest.mark.asyncio
async def test_summarize_descriptions_warns_once_and_falls_back(monkeypatch):
    warnings: list[str] = []

    operate_module._UTILITY_LLM_FALLBACK_WARNED.clear()

    def _capture_warning(message, *args):
        warnings.append(str(message) % args if args else str(message))

    monkeypatch.setattr(operate_module.logger, "warning", _capture_warning)

    first = await _summarize_descriptions(
        "entity",
        "BYD",
        ["electric vehicle maker", "battery supplier"],
        _global_config(),
        llm_response_cache=None,
    )
    second = await _summarize_descriptions(
        "entity",
        "BYD",
        ["electric vehicle maker", "battery supplier"],
        _global_config(),
        llm_response_cache=None,
    )

    assert first == "main-summary"
    assert second == "main-summary"
    assert sum("Utility LLM is not configured" in item for item in warnings) == 1
