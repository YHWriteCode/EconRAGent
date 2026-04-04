from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

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
        target_urls: list[str] = []
        page_limit = max_pages or self.config.max_pages

        for url in urls:
            if len(target_urls) >= page_limit or len(pages) >= page_limit:
                break
            expanded_urls, feed_error = await self._expand_feed_url(
                url,
                remaining=page_limit - len(target_urls),
            )
            if expanded_urls is not None:
                if not expanded_urls:
                    pages.append(
                        CrawledPage(
                            url=url,
                            success=False,
                            final_url=url,
                            error=feed_error or "Feed source did not yield crawlable item links",
                        )
                    )
                    continue
                target_urls.extend(expanded_urls)
                continue
            target_urls.append(url)

        for url in target_urls[: max(0, page_limit - len(pages))]:
            pages.append(
                await self.crawl_url(
                    url,
                    max_content_chars=max_content_chars,
                )
            )
        return pages[:page_limit]

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

    async def discover_feed_urls(
        self,
        feed_url: str,
        *,
        top_k: int = 5,
    ) -> list[DiscoveredUrl]:
        payload, final_url = await self._fetch_url_text(feed_url)
        return self._parse_feed_entries(payload, base_url=final_url or feed_url, top_k=top_k)

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

    async def _expand_feed_url(
        self,
        url: str,
        *,
        remaining: int,
    ) -> tuple[list[str] | None, str | None]:
        if remaining <= 0:
            return [], None
        if not self._looks_like_feed_url(url):
            return None, None
        try:
            discovered = await self.discover_feed_urls(url, top_k=remaining)
        except Exception as exc:
            logger.warning("Failed to expand feed URL '%s': %s", url, exc)
            return [], str(exc)
        return [item.url for item in discovered[:remaining]], None

    async def _fetch_url_text(self, url: str) -> tuple[str, str]:
        timeout_seconds = max(1, int(self.config.page_timeout_ms / 1000))

        def _read() -> tuple[str, str]:
            request = Request(
                url,
                headers={"User-Agent": "kg-agent-feed-reader/1.0"},
            )
            with urlopen(request, timeout=timeout_seconds) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = response.read().decode(charset, errors="replace")
                return payload, response.geturl()

        return await asyncio.to_thread(_read)

    @classmethod
    def _parse_feed_entries(
        cls,
        payload: str,
        *,
        base_url: str,
        top_k: int,
    ) -> list[DiscoveredUrl]:
        if not payload.strip():
            return []
        root = ET.fromstring(payload)
        root_name = cls._xml_local_name(root.tag)
        entries: list[DiscoveredUrl] = []
        seen: set[str] = set()

        if root_name == "rss":
            for item in root.findall(".//item"):
                url = cls._extract_rss_item_url(item, base_url)
                if not url or url in seen:
                    continue
                seen.add(url)
                entries.append(
                    DiscoveredUrl(
                        title=(item.findtext("title") or url).strip(),
                        url=url,
                        source="rss",
                    )
                )
                if len(entries) >= top_k:
                    break
            return entries

        if root_name == "feed":
            for entry in root.findall(".//{*}entry"):
                url = cls._extract_atom_entry_url(entry, base_url)
                if not url or url in seen:
                    continue
                seen.add(url)
                title = ""
                title_node = entry.find("{*}title")
                if title_node is not None and title_node.text:
                    title = title_node.text.strip()
                entries.append(
                    DiscoveredUrl(
                        title=title or url,
                        url=url,
                        source="atom",
                    )
                )
                if len(entries) >= top_k:
                    break
            return entries

        return []

    @staticmethod
    def _extract_rss_item_url(item: ET.Element, base_url: str) -> str | None:
        candidates = [
            item.findtext("link"),
            item.findtext("{*}link"),
            item.findtext("{*}guid"),
        ]
        for candidate in candidates:
            normalized = (candidate or "").strip()
            if normalized.startswith("http://") or normalized.startswith("https://"):
                return normalized
            if normalized:
                return urljoin(base_url, normalized)
        return None

    @staticmethod
    def _extract_atom_entry_url(entry: ET.Element, base_url: str) -> str | None:
        for link_node in entry.findall("{*}link"):
            href = (link_node.get("href") or "").strip()
            rel = (link_node.get("rel") or "alternate").strip().lower()
            if not href or rel not in {"alternate", ""}:
                continue
            return href if href.startswith(("http://", "https://")) else urljoin(base_url, href)
        return None

    @staticmethod
    def _xml_local_name(tag: str) -> str:
        return (tag or "").rsplit("}", 1)[-1].lower()

    @staticmethod
    def _looks_like_feed_url(url: str) -> bool:
        normalized = (url or "").strip().lower()
        return any(
            marker in normalized
            for marker in (
                "/feed",
                "/rss",
                "rss.xml",
                "atom.xml",
                "feed.xml",
                "format=rss",
                "format=atom",
            )
        )

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
