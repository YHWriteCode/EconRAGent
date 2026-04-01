from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from kg_agent.config import CrawlerConfig
from kg_agent.crawler.content_extractor import (
    build_search_url,
    build_excerpt,
    extract_search_results_from_markdown,
    extract_markdown_text,
    normalize_links,
    sanitize_plain_text,
)

logger = logging.getLogger(__name__)


@dataclass
class CrawledPage:
    url: str
    success: bool
    final_url: str | None = None
    title: str | None = None
    markdown: str = ""
    excerpt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscoveredUrl:
    title: str
    url: str
    source: str = "duckduckgo"
    score: float | None = None
    match_count: int = 0
    article_like: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Crawl4AIAdapter:
    def __init__(self, *, config: CrawlerConfig | None = None):
        self.config = config or CrawlerConfig.from_env()
        self._crawler = None
        self._crawler_lock = asyncio.Lock()

    def is_available(self) -> bool:
        return self.config.provider == "crawl4ai"

    async def start(self) -> None:
        if self._crawler is not None:
            return

        async with self._crawler_lock:
            if self._crawler is not None:
                return

            crawl4ai = self._import_crawl4ai()
            browser_channel = self._resolve_browser_channel()
            browser_config = crawl4ai.BrowserConfig(
                browser_type=self.config.browser_type,
                channel=browser_channel,
                chrome_channel=browser_channel,
                headless=self.config.headless,
                verbose=self.config.verbose,
            )
            crawler = crawl4ai.AsyncWebCrawler(config=browser_config)
            await crawler.start()
            self._crawler = crawler

    async def close(self) -> None:
        crawler = self._crawler
        self._crawler = None
        if crawler is None:
            return
        await crawler.close()

    async def crawl_url(
        self,
        url: str,
        *,
        max_content_chars: int | None = None,
    ) -> CrawledPage:
        crawl4ai = self._import_crawl4ai()
        await self.start()
        run_config = crawl4ai.CrawlerRunConfig(
            cache_mode=self._resolve_cache_mode(crawl4ai),
            word_count_threshold=self.config.word_count_threshold,
            page_timeout=self.config.page_timeout_ms,
            verbose=False,
            log_console=False,
        )
        result = await self._crawler.arun(url=url, config=run_config)
        return self._normalize_result(
            url=url,
            result=result,
            max_content_chars=max_content_chars or self.config.max_content_chars,
        )

    async def crawl_urls(
        self,
        urls: list[str],
        *,
        max_pages: int | None = None,
        max_content_chars: int | None = None,
    ) -> list[CrawledPage]:
        pages: list[CrawledPage] = []
        for url in urls[: max_pages or self.config.max_pages]:
            pages.append(
                await self.crawl_url(
                    url,
                    max_content_chars=max_content_chars,
                )
            )
        return pages

    async def discover_urls(
        self,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[DiscoveredUrl]:
        search_url = build_search_url(self.config.search_engine, query)
        search_page = await self.crawl_url(
            search_url,
            max_content_chars=max(self.config.max_content_chars, 8000),
        )
        if not search_page.success:
            return []

        discovered = extract_search_results_from_markdown(
            search_page.markdown,
            query=query,
            top_k=top_k,
        )
        return [
            DiscoveredUrl(
                title=item["title"],
                url=item["url"],
                source=self.config.search_engine,
                score=item.get("score"),
                match_count=int(item.get("match_count", 0)),
                article_like=bool(item.get("article_like", False)),
            )
            for item in discovered
        ]

    @staticmethod
    def _import_crawl4ai():
        try:
            import crawl4ai  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "crawl4ai is not installed. Run 'uv sync --active --extra api' and "
                "then 'crawl4ai-setup' before using the crawler."
            ) from exc
        return crawl4ai

    def _resolve_cache_mode(self, crawl4ai: Any):
        cache_mode_name = (self.config.cache_mode or "BYPASS").strip().upper()
        cache_mode = getattr(getattr(crawl4ai, "CacheMode", object), cache_mode_name, None)
        if cache_mode is not None:
            return cache_mode
        return getattr(crawl4ai.CacheMode, "BYPASS")

    def _resolve_browser_channel(self) -> str:
        configured = (self.config.browser_channel or "").strip()
        if configured:
            return configured

        if (self.config.browser_type or "chromium").strip().lower() != "chromium":
            return ""

        if self._has_playwright_browser_runtime():
            return "chromium"

        windows_browser_paths = [
            ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            ("msedge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ]
        for channel, browser_path in windows_browser_paths:
            if os.path.exists(browser_path):
                logger.warning(
                    "Playwright browser runtime is unavailable; falling back to system browser channel '%s'.",
                    channel,
                )
                return channel

        logger.warning(
            "Playwright browser runtime is unavailable and no system Edge/Chrome browser was found; falling back to Playwright chromium channel."
        )
        return "chromium"

    @staticmethod
    def _playwright_browser_root() -> str:
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return os.path.join(local_app_data, "ms-playwright")
        return r"C:\Users\15770\AppData\Local\ms-playwright"

    @classmethod
    def _has_playwright_browser_runtime(cls) -> bool:
        """Check whether the *specific* Playwright chromium build expected by
        the installed ``playwright`` package is available on disk."""
        try:
            from playwright._impl._driver import compute_driver_executable  # type: ignore
            import subprocess, json as _json

            driver_exec, driver_cli = compute_driver_executable()
            proc = subprocess.run(
                [str(driver_exec), str(driver_cli), "install", "--dry-run"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # Dry-run prints JSON with expected browser paths; fallback below.
        except Exception:
            pass

        # Fallback: simply check if the Playwright executable path resolves.
        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            with sync_playwright() as p:
                path = p.chromium.executable_path
                if path and os.path.isfile(path):
                    return True
        except Exception:
            pass

        return False

    @staticmethod
    def _normalize_result(
        *,
        url: str,
        result: Any,
        max_content_chars: int,
    ) -> CrawledPage:
        success = bool(getattr(result, "success", False))
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}

        title = metadata.get("title") or getattr(result, "title", None)
        markdown = extract_markdown_text(getattr(result, "markdown", ""))
        if max_content_chars > 0:
            markdown = markdown[:max_content_chars]
        markdown = sanitize_plain_text(markdown)
        excerpt = build_excerpt(markdown, max_chars=min(max_content_chars, 320))

        final_url = getattr(result, "url", None) or metadata.get("url") or url
        links = normalize_links(getattr(result, "links", None))
        error = None if success else (
            getattr(result, "error_message", None)
            or getattr(result, "error", None)
            or "Crawl failed"
        )

        return CrawledPage(
            url=url,
            success=success,
            final_url=final_url,
            title=title,
            markdown=markdown,
            excerpt=excerpt,
            metadata=metadata,
            links=links,
            error=error,
        )
