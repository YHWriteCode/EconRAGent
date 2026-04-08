import logging
from types import SimpleNamespace

import pytest

from kg_agent.config import CrawlerConfig
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter
from kg_agent.crawler.content_extractor import extract_search_results_from_markdown


def test_crawler_adapter_prefers_configured_browser_channel():
    adapter = Crawl4AIAdapter(
        config=CrawlerConfig(browser_channel="msedge", browser_type="chromium")
    )

    assert adapter._resolve_browser_channel() == "msedge"


def test_crawler_adapter_non_chromium_browser_uses_empty_channel():
    adapter = Crawl4AIAdapter(
        config=CrawlerConfig(browser_type="firefox", browser_channel="")
    )

    assert adapter._resolve_browser_channel() == ""


def test_crawler_adapter_prefers_playwright_runtime_by_default(monkeypatch):
    adapter = Crawl4AIAdapter(config=CrawlerConfig(browser_type="chromium"))

    monkeypatch.setattr(adapter, "_has_playwright_browser_runtime", lambda: True)

    assert adapter._resolve_browser_channel() == "chromium"


def test_crawler_adapter_falls_back_to_edge_with_warning(monkeypatch, caplog):
    adapter = Crawl4AIAdapter(config=CrawlerConfig(browser_type="chromium"))

    monkeypatch.setattr(adapter, "_has_playwright_browser_runtime", lambda: False)

    def fake_exists(path: str) -> bool:
        return "msedge.exe" in path

    monkeypatch.setattr("kg_agent.crawler.crawler_adapter.os.path.exists", fake_exists)

    with caplog.at_level(logging.WARNING):
        channel = adapter._resolve_browser_channel()

    assert channel == "msedge"
    assert "Playwright browser runtime is unavailable" in caplog.text


def test_crawler_adapter_builds_llm_extraction_strategy(monkeypatch):
    captured = {}

    class _FakeLLMConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeLLMExtractionStrategy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeCrawlerRunConfig:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_crawl4ai = SimpleNamespace(
        CacheMode=SimpleNamespace(BYPASS="BYPASS"),
        LLMConfig=_FakeLLMConfig,
        LLMExtractionStrategy=_FakeLLMExtractionStrategy,
        CrawlerRunConfig=_FakeCrawlerRunConfig,
    )
    adapter = Crawl4AIAdapter(
        config=CrawlerConfig(
            llm_extraction_enabled=True,
            llm_extraction_provider="openai/test-model",
            llm_extraction_api_token="test-token",
            llm_extraction_base_url="https://example.com/v1",
            llm_extraction_instruction="Extract the key facts",
            llm_extraction_input_format="markdown",
            llm_extraction_type="block",
            llm_extraction_force_json_response=True,
            llm_extraction_apply_chunking=False,
        )
    )

    monkeypatch.setattr(adapter, "_import_crawl4ai", lambda: fake_crawl4ai)

    adapter._build_run_config()

    strategy = captured["extraction_strategy"]
    assert strategy.kwargs["instruction"] == "Extract the key facts"
    assert strategy.kwargs["input_format"] == "markdown"
    assert strategy.kwargs["extraction_type"] == "block"
    assert strategy.kwargs["force_json_response"] is True
    assert strategy.kwargs["apply_chunking"] is False
    assert strategy.kwargs["llm_config"].kwargs == {
        "provider": "openai/test-model",
        "api_token": "test-token",
        "base_url": "https://example.com/v1",
    }


def test_crawler_adapter_prefers_llm_extracted_content_when_configured():
    page = Crawl4AIAdapter._normalize_result(
        url="https://example.com/a",
        result=SimpleNamespace(
            success=True,
            metadata={},
            title="Example",
            markdown="Original markdown body",
            extracted_content='[{"title":"Structured title","content":"Structured summary"}]',
            links=[],
            redirected_url="https://example.com/a",
        ),
        max_content_chars=4000,
        prefer_extracted_content=True,
    )

    assert page.success is True
    assert page.markdown == "Structured title\n\nStructured summary"
    assert page.metadata["content_source"] == "crawl4ai_llm_extraction"
    assert page.metadata["llm_extraction_applied"] is True


def test_extract_search_results_from_duckduckgo_markdown():
    markdown = """
##  [Overview of Chinese new energy vehicle industry](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.sciencedirect.com%2Fpaper)
[Snippet](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.sciencedirect.com%2Fpaper)
##  [BYD annual report](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.byd.com%2Freport)
"""

    results = extract_search_results_from_markdown(
        markdown,
        query="BYD annual report",
        top_k=5,
    )

    assert results[0]["title"] == "BYD annual report"
    assert results[0]["url"] == "https://www.byd.com/report"
    assert results[0]["score"] >= results[1]["score"]


def test_extract_search_results_filters_social_and_prefers_article_pages():
    markdown = """
## [BYD latest updates topic page](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcarnewschina.com%2Ftopics%2Fbyd%2F)
## [BYD - Latest news and updates](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcnevpost.com%2Fbyd%2F)
## [BYD launches new hybrid EV](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcnevpost.com%2F2026%2F03%2F21%2Fbyd-launches-new-hybrid-ev%2F)
## [Share this on X](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fintent%2Ftweet%3Ftext%3Dbyd)
"""

    results = extract_search_results_from_markdown(
        markdown,
        query="latest BYD hybrid EV news",
        top_k=5,
    )

    assert results[0]["url"] == "https://cnevpost.com/2026/03/21/byd-launches-new-hybrid-ev"
    assert {item["url"] for item in results[1:]} == {
        "https://cnevpost.com/byd",
        "https://carnewschina.com/topics/byd",
    }
    assert all("x.com" not in item["url"] for item in results)


def test_extract_search_results_falls_back_when_only_listing_pages_exist():
    markdown = """
## [BYD latest news](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcarnewschina.com%2Ftopics%2Fbyd%2F)
## [BYD - Latest news and updates](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcnevpost.com%2Fbyd%2F)
## [News list - BYD](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.byd.com%2Fus%2Fnews-list%2F)
"""

    results = extract_search_results_from_markdown(
        markdown,
        query="latest BYD policy news",
        top_k=5,
    )

    assert [item["url"] for item in results] == [
        "https://carnewschina.com/topics/byd",
        "https://www.byd.com/us/news-list",
        "https://cnevpost.com/byd",
    ]
