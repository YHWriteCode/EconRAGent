from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from kg_agent.config import CrawlerConfig
from kg_agent.crawler.content_extractor import (
    build_search_url,
    build_excerpt,
    canonicalize_url,
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
    published_at: str | None = None
    author: str | None = None
    categories: list[str] = field(default_factory=list)

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
        await self.start()
        run_config = self._build_run_config()
        result = await self._crawler.arun(url=url, config=run_config)
        return self._normalize_result(
            url=url,
            result=result,
            max_content_chars=max_content_chars or self.config.max_content_chars,
            prefer_extracted_content=self.config.llm_extraction_prefer_content,
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

        remaining_target_urls = target_urls[: max(0, page_limit - len(pages))]
        if remaining_target_urls:
            pages.extend(
                await self._crawl_many_article_urls(
                    remaining_target_urls,
                    max_content_chars=max_content_chars,
                )
            )
        return pages[:page_limit]

    async def _crawl_many_article_urls(
        self,
        urls: list[str],
        *,
        max_content_chars: int | None = None,
    ) -> list[CrawledPage]:
        if not urls:
            return []
        if type(self).crawl_url is not Crawl4AIAdapter.crawl_url:
            pages: list[CrawledPage] = []
            for url in urls:
                pages.append(
                    await self.crawl_url(
                        url,
                        max_content_chars=max_content_chars,
                    )
                )
            return pages

        await self.start()
        run_config = self._build_run_config()
        results = await self._crawler.arun_many(urls=urls, config=run_config)
        if not isinstance(results, list):
            results = [item async for item in results]

        results_by_requested_url: dict[str, list[Any]] = {}
        for result in results:
            request_url = str(getattr(result, "url", "") or "").strip()
            if not request_url:
                continue
            results_by_requested_url.setdefault(request_url, []).append(result)

        pages: list[CrawledPage] = []
        for request_url in urls:
            bucket = results_by_requested_url.get(request_url, [])
            if not bucket:
                pages.append(
                    CrawledPage(
                        url=request_url,
                        final_url=request_url,
                        success=False,
                        error="Batch crawl did not return a result for the requested URL",
                    )
                )
                continue
            result = bucket.pop(0)
            pages.append(
                self._normalize_result(
                    url=request_url,
                    result=result,
                    max_content_chars=max_content_chars or self.config.max_content_chars,
                    prefer_extracted_content=self.config.llm_extraction_prefer_content,
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

    def _build_run_config(self):
        crawl4ai = self._import_crawl4ai()
        extraction_strategy = self._build_extraction_strategy(crawl4ai)
        return crawl4ai.CrawlerRunConfig(
            cache_mode=self._resolve_cache_mode(crawl4ai),
            word_count_threshold=self.config.word_count_threshold,
            page_timeout=self.config.page_timeout_ms,
            extraction_strategy=extraction_strategy,
            verbose=False,
            log_console=False,
        )

    def _build_extraction_strategy(self, crawl4ai: Any):
        if not self.config.llm_extraction_enabled:
            return None
        provider = (self.config.llm_extraction_provider or "").strip()
        if not provider:
            raise RuntimeError(
                "Crawl4AI LLM extraction is enabled but no provider is configured. "
                "Set KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_PROVIDER "
                "(for example openai/gpt-4o-mini or openai/<your-model>)."
            )

        llm_config = crawl4ai.LLMConfig(
            provider=provider,
            api_token=(self.config.llm_extraction_api_token or "").strip() or None,
            base_url=(self.config.llm_extraction_base_url or "").strip() or None,
        )
        schema = self._load_extraction_schema()
        extraction_type = (
            self.config.llm_extraction_type.strip().lower() or "block"
        )
        input_format = (
            self.config.llm_extraction_input_format.strip().lower() or "markdown"
        )
        return crawl4ai.LLMExtractionStrategy(
            llm_config=llm_config,
            instruction=(self.config.llm_extraction_instruction or "").strip() or None,
            schema=schema,
            extraction_type=extraction_type,
            input_format=input_format,
            force_json_response=self.config.llm_extraction_force_json_response,
            apply_chunking=self.config.llm_extraction_apply_chunking,
            verbose=False,
        )

    def _load_extraction_schema(self) -> dict[str, Any] | None:
        schema_json = (self.config.llm_extraction_schema_json or "").strip()
        if not schema_json:
            return None
        try:
            payload = json.loads(schema_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_SCHEMA_JSON must be valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_SCHEMA_JSON must decode to a JSON object"
            )
        return payload

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
                url = canonicalize_url(cls._extract_rss_item_url(item, base_url) or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                entries.append(
                    DiscoveredUrl(
                        title=(item.findtext("title") or url).strip(),
                        url=url,
                        source="rss",
                        published_at=cls._extract_rss_item_published_at(item),
                        author=cls._extract_rss_item_author(item),
                        categories=cls._extract_rss_item_categories(item),
                    )
                )
                if len(entries) >= top_k:
                    break
            return entries

        if root_name == "feed":
            for entry in root.findall(".//{*}entry"):
                url = canonicalize_url(cls._extract_atom_entry_url(entry, base_url) or "")
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
                        published_at=cls._extract_atom_entry_published_at(entry),
                        author=cls._extract_atom_entry_author(entry),
                        categories=cls._extract_atom_entry_categories(entry),
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

    @classmethod
    def _extract_rss_item_published_at(cls, item: ET.Element) -> str | None:
        candidates = [
            item.findtext("pubDate"),
            item.findtext("{*}pubDate"),
            item.findtext("{http://purl.org/dc/elements/1.1/}date"),
            item.findtext("{*}date"),
            item.findtext("published"),
            item.findtext("{*}published"),
            item.findtext("updated"),
            item.findtext("{*}updated"),
        ]
        for candidate in candidates:
            normalized = cls._normalize_datetime_text(candidate)
            if normalized:
                return normalized
        return None

    @classmethod
    def _extract_atom_entry_published_at(cls, entry: ET.Element) -> str | None:
        candidates = [
            entry.findtext("{*}updated"),
            entry.findtext("{*}published"),
        ]
        for candidate in candidates:
            normalized = cls._normalize_datetime_text(candidate)
            if normalized:
                return normalized
        return None

    @staticmethod
    def _normalize_datetime_text(value: str | None) -> str | None:
        text = (value or "").strip()
        if not text:
            return None

        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            parsed = None
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _normalize_metadata_values(values: list[str | None]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = " ".join((value or "").strip().split())
            if not item:
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            normalized.append(item)
            seen.add(lowered)
        return normalized

    @classmethod
    def _extract_rss_item_author(cls, item: ET.Element) -> str | None:
        candidates = [
            item.findtext("author"),
            item.findtext("{*}author"),
            item.findtext("{http://purl.org/dc/elements/1.1/}creator"),
            item.findtext("{*}creator"),
        ]
        values = cls._normalize_metadata_values(candidates)
        return values[0] if values else None

    @classmethod
    def _extract_rss_item_categories(cls, item: ET.Element) -> list[str]:
        values: list[str | None] = []
        for node in item.findall("category"):
            values.append(node.text)
        for node in item.findall("{*}category"):
            values.append(node.text or node.get("term"))
        return cls._normalize_metadata_values(values)

    @classmethod
    def _extract_atom_entry_author(cls, entry: ET.Element) -> str | None:
        candidates = [
            entry.findtext("{*}author/{*}name"),
            entry.findtext("{*}author/{*}email"),
            entry.findtext("{*}author"),
        ]
        values = cls._normalize_metadata_values(candidates)
        return values[0] if values else None

    @classmethod
    def _extract_atom_entry_categories(cls, entry: ET.Element) -> list[str]:
        values: list[str | None] = []
        for node in entry.findall("{*}category"):
            values.append(node.get("term") or node.get("label") or node.text)
        return cls._normalize_metadata_values(values)

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
        prefer_extracted_content: bool = False,
    ) -> CrawledPage:
        success = bool(getattr(result, "success", False))
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}

        title = metadata.get("title") or getattr(result, "title", None)
        markdown = extract_markdown_text(getattr(result, "markdown", ""))
        extracted_content = Crawl4AIAdapter._extract_llm_extracted_text(
            getattr(result, "extracted_content", None)
        )
        if prefer_extracted_content and extracted_content:
            markdown = extracted_content
            metadata = {
                **metadata,
                "content_source": "crawl4ai_llm_extraction",
                "llm_extraction_applied": True,
            }
        elif extracted_content:
            metadata = {
                **metadata,
                "content_source": "crawl4ai_markdown",
                "llm_extraction_applied": True,
            }
        if max_content_chars > 0:
            markdown = markdown[:max_content_chars]
        markdown = sanitize_plain_text(markdown)
        excerpt = build_excerpt(markdown, max_chars=min(max_content_chars, 320))

        final_url = (
            getattr(result, "redirected_url", None)
            or getattr(result, "url", None)
            or metadata.get("url")
            or url
        )
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

    @classmethod
    def _extract_llm_extracted_text(cls, extracted_content: Any) -> str:
        text = str(extracted_content or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        parts = cls._flatten_extracted_payload(payload)
        if not parts:
            return text
        return "\n\n".join(parts)

    @classmethod
    def _flatten_extracted_payload(cls, payload: Any) -> list[str]:
        if payload is None:
            return []
        if isinstance(payload, str):
            normalized = payload.strip()
            return [normalized] if normalized else []
        if isinstance(payload, (int, float, bool)):
            return [str(payload)]
        if isinstance(payload, list):
            parts: list[str] = []
            for item in payload:
                parts.extend(cls._flatten_extracted_payload(item))
            return cls._deduplicate_text_parts(parts)
        if isinstance(payload, dict):
            parts: list[str] = []
            for key in (
                "title",
                "heading",
                "summary",
                "content",
                "text",
                "body",
                "description",
            ):
                if key in payload:
                    parts.extend(cls._flatten_extracted_payload(payload[key]))
            for key, value in payload.items():
                if key in {
                    "title",
                    "heading",
                    "summary",
                    "content",
                    "text",
                    "body",
                    "description",
                }:
                    continue
                parts.extend(cls._flatten_extracted_payload(value))
            return cls._deduplicate_text_parts(parts)
        return []

    @staticmethod
    def _deduplicate_text_parts(parts: list[str]) -> list[str]:
        normalized_parts: list[str] = []
        seen: set[str] = set()
        for part in parts:
            normalized = " ".join((part or "").split())
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized_parts.append(normalized)
        return normalized_parts
