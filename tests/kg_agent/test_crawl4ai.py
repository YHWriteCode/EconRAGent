import asyncio

import pytest
from crawl4ai import AsyncWebCrawler, BrowserConfig

from kg_agent.config import CrawlerConfig
from kg_agent.crawler.content_extractor import extract_markdown_text
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter


def _build_browser_config() -> BrowserConfig:
    adapter = Crawl4AIAdapter(
        config=CrawlerConfig(
            provider="crawl4ai",
            browser_type="chromium",
            headless=True,
            verbose=False,
        )
    )
    has_playwright_runtime = adapter._has_playwright_browser_runtime()
    browser_channel = adapter._resolve_browser_channel()
    if not has_playwright_runtime and browser_channel == "chromium":
        pytest.skip(
            "Playwright browser runtime is unavailable and no system Chrome/Edge browser was found"
        )
    return BrowserConfig(
        browser_type="chromium",
        channel=browser_channel,
        chrome_channel=browser_channel,
        headless=True,
        verbose=False,
    )


def test_crawl4ai_local_html_smoke():
    html = """
    <html>
      <head><title>Crawl4AI Smoke</title></head>
      <body>
        <h1>BYD Battery Expansion</h1>
        <p>Policy support helped BYD expand battery production.</p>
      </body>
    </html>
    """

    async def main():
        async with AsyncWebCrawler(config=_build_browser_config()) as crawler:
            result = await crawler.arun(url=f"raw:{html}")
            markdown = extract_markdown_text(result.markdown)
            assert result.success is True
            assert "BYD Battery Expansion" in markdown
            assert "Policy support helped BYD expand battery production." in markdown

    asyncio.run(main())
