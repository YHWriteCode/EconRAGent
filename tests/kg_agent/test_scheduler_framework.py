import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kg_agent.config import CrawlerConfig
from kg_agent.crawler.crawl_state_store import CrawlStateRecord, JsonCrawlStateStore
from kg_agent.crawler.crawl_state_store import SqliteCrawlStateStore
from kg_agent.crawler.crawler_adapter import Crawl4AIAdapter, CrawledPage
from kg_agent.crawler.scheduler import (
    IngestScheduler,
    LocalSchedulerCoordinator,
)
from kg_agent.crawler.source_registry import (
    JsonSourceRegistry,
    MonitoredSource,
    SqliteSourceRegistry,
)


class _FakeRAG:
    def __init__(self):
        self.calls: list[dict] = []

    async def ainsert(self, input, file_paths=None, metadatas=None):
        self.calls.append(
            {"input": input, "file_paths": file_paths, "metadatas": metadatas}
        )
        return f"track-{len(self.calls)}"


class _FakeChunksVectorDB:
    def __init__(self, results: list[dict] | None = None):
        self.results = list(results or [])
        self.calls: list[dict] = []

    def set_results(self, results: list[dict]):
        self.results = list(results)

    async def query(self, query: str, top_k: int, query_embedding=None):
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "query_embedding": query_embedding,
            }
        )
        return list(self.results[:top_k])


class _LifecycleRAG:
    def __init__(self, *, chunks_vdb=None, workspace: str = ""):
        self.insert_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.chunks_vdb = chunks_vdb
        self.workspace = workspace

    async def ainsert(self, input, file_paths=None, ids=None, metadatas=None):
        self.insert_calls.append(
            {
                "input": input,
                "file_paths": file_paths,
                "ids": ids,
                "metadatas": metadatas,
            }
        )
        return f"track-{len(self.insert_calls)}"

    async def adelete_by_doc_id(self, doc_id: str):
        self.delete_calls.append(doc_id)
        return {"status": "success", "doc_id": doc_id}


class _NoDeleteLifecycleRAG:
    def __init__(self, *, workspace: str = ""):
        self.insert_calls: list[dict] = []
        self.workspace = workspace

    async def ainsert(self, input, file_paths=None, ids=None, metadatas=None):
        self.insert_calls.append(
            {
                "input": input,
                "file_paths": file_paths,
                "ids": ids,
                "metadatas": metadatas,
            }
        )
        return f"track-{len(self.insert_calls)}"


class _UtilityEventClusterLLM:
    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.calls: list[dict] = []

    def is_available(self):
        return True

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.payload)


class _FakeCrawlerAdapter:
    def __init__(self, batches):
        self._batches = list(batches)

    async def crawl_urls(self, urls, *, max_pages=None):
        return self._batches.pop(0)


class _BlockingCrawlerAdapter:
    def __init__(self, pages):
        self._pages = pages
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def crawl_urls(self, urls, *, max_pages=None):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return self._pages


class _FeedCrawlerAdapter(Crawl4AIAdapter):
    def __init__(self, feed_payload: str):
        super().__init__(config=CrawlerConfig(provider="crawl4ai"))
        self._feed_payload = feed_payload

    async def _fetch_url_text(self, url: str) -> tuple[str, str]:
        return self._feed_payload, url

    async def crawl_url(self, url: str, *, max_content_chars: int | None = None) -> CrawledPage:
        del max_content_chars
        article_slug = url.rstrip("/").rsplit("/", 1)[-1]
        slug_text = (
            article_slug.replace("-", " ")
            .replace("0", " zero ")
            .replace("1", " one ")
            .replace("2", " two ")
            .replace("3", " three ")
            .replace("4", " four ")
            .replace("5", " five ")
            .replace("6", " six ")
            .replace("7", " seven ")
            .replace("8", " eight ")
            .replace("9", " nine ")
        )
        return _page(f"content for article {slug_text}", url=url)


class _SequentialFeedCrawlerAdapter(_FeedCrawlerAdapter):
    def __init__(self, feed_payloads: list[str]):
        super().__init__(feed_payloads[0])
        self._feed_payloads = list(feed_payloads)
        self._feed_index = 0

    async def _fetch_url_text(self, url: str) -> tuple[str, str]:
        if not self._feed_payloads:
            return "", url
        index = min(self._feed_index, len(self._feed_payloads) - 1)
        self._feed_index += 1
        return self._feed_payloads[index], url


class _MappedFeedCrawlerAdapter(_FeedCrawlerAdapter):
    def __init__(self, payload_by_url: dict[str, str]):
        first_payload = next(iter(payload_by_url.values()), "")
        super().__init__(first_payload)
        self._payload_by_url = dict(payload_by_url)

    async def _fetch_url_text(self, url: str) -> tuple[str, str]:
        return self._payload_by_url.get(url, ""), url


class _MappedFeedPageCrawlerAdapter(_MappedFeedCrawlerAdapter):
    def __init__(self, payload_by_url: dict[str, str], page_content_by_url: dict[str, str]):
        super().__init__(payload_by_url)
        self._page_content_by_url = dict(page_content_by_url)

    async def crawl_url(self, url: str, *, max_content_chars: int | None = None) -> CrawledPage:
        del max_content_chars
        return _page(self._page_content_by_url.get(url, f"content for {url}"), url=url)


class _MappedFeedPageMetadataCrawlerAdapter(_MappedFeedCrawlerAdapter):
    def __init__(
        self,
        payload_by_url: dict[str, str],
        page_content_by_url: dict[str, str],
        page_title_by_url: dict[str, str],
    ):
        super().__init__(payload_by_url)
        self._page_content_by_url = dict(page_content_by_url)
        self._page_title_by_url = dict(page_title_by_url)

    async def crawl_url(self, url: str, *, max_content_chars: int | None = None) -> CrawledPage:
        del max_content_chars
        return _page(
            self._page_content_by_url.get(url, f"content for {url}"),
            url=url,
            title=self._page_title_by_url.get(url, "News"),
        )


class _FinalUrlVariantFeedCrawlerAdapter(_FeedCrawlerAdapter):
    def __init__(self, feed_payload: str, final_urls: list[str], *, markdown: str = "same content"):
        super().__init__(feed_payload)
        self._final_urls = list(final_urls)
        self._markdown = markdown

    async def crawl_url(self, url: str, *, max_content_chars: int | None = None) -> CrawledPage:
        del max_content_chars
        if self._final_urls:
            final_url = self._final_urls.pop(0)
        else:
            final_url = url
        return CrawledPage(
            url=url,
            final_url=final_url,
            success=True,
            title="News",
            markdown=self._markdown,
            excerpt=self._markdown[:20],
        )


class _BatchOnlyFeedCrawlerAdapter(_MappedFeedCrawlerAdapter):
    def __init__(self, payload_by_url: dict[str, str], page_content_by_url: dict[str, str]):
        super().__init__(payload_by_url)
        self._page_content_by_url = dict(page_content_by_url)
        self.batch_calls: list[dict[str, object]] = []

    async def crawl_urls(
        self,
        urls,
        *,
        max_pages=None,
        max_content_chars=None,
    ):
        self.batch_calls.append(
            {
                "urls": list(urls),
                "max_pages": max_pages,
                "max_content_chars": max_content_chars,
            }
        )
        limited_urls = list(urls[:max_pages]) if max_pages is not None else list(urls)
        return [
            _page(
                self._page_content_by_url.get(url, f"content for {url}"),
                url=url,
            )
            for url in limited_urls
        ]

    async def crawl_url(self, url: str, *, max_content_chars: int | None = None) -> CrawledPage:
        del url, max_content_chars
        raise AssertionError("feed article crawling should use crawl_urls() batching")


def _page(
    markdown: str,
    *,
    url: str = "https://example.com/news",
    title: str = "News",
) -> CrawledPage:
    return CrawledPage(
        url=url,
        final_url=url,
        success=True,
        title=title,
        markdown=markdown,
        excerpt=markdown[:20],
    )


@pytest.mark.asyncio
async def test_source_and_state_json_reload_from_disk(tmp_path: Path):
    source_file = tmp_path / "sources.json"
    state_file = tmp_path / "state.json"

    registry = JsonSourceRegistry(str(source_file))
    store = JsonCrawlStateStore(str(state_file))

    source = MonitoredSource(
        source_id="news-source",
        name="News Source",
        urls=["https://example.com/news"],
        interval_seconds=120,
        max_pages=2,
        source_type="page",
        schedule_mode="fixed",
        feed_filter={
            "include_patterns": ["battery"],
            "exclude_patterns": ["rumor"],
            "include_authors": ["analyst"],
            "exclude_categories": ["gossip"],
            "allowed_domains": ["example.com"],
            "max_age_days": 7,
        },
        feed_retention={"mode": "latest", "max_items": 3, "max_age_days": 30},
        feed_priority={
            "mode": "auto",
            "priority_patterns": ["earnings"],
            "preferred_domains": ["priority.example.com"],
            "preferred_authors": ["chief"],
            "preferred_categories": ["supply chain"],
        },
        feed_dedup={"mode": "content_signature", "signature_token_limit": 48},
        content_lifecycle={
            "content_class": "short_term_news",
            "update_mode": "replace_latest",
            "ttl_days": 2,
            "delete_expired": True,
            "event_cluster_mode": "heuristic_llm",
            "event_cluster_window_days": 2.5,
            "event_cluster_min_similarity": 0.81,
        },
    )
    await registry.upsert_source(source)
    await store.put_record(
        CrawlStateRecord(
            source_id="news-source",
            last_status="success",
            recent_item_keys=["https://example.com/news"],
            item_published_at={"https://example.com/news": "2026-04-01T00:00:00+00:00"},
            item_content_fingerprints={"https://example.com/news": "fingerprint-1"},
            item_active_doc_ids={"https://example.com/news": "doc-active-1"},
            item_event_cluster_ids={"https://example.com/news": "cluster-news-1"},
            event_clusters={
                "cluster-news-1": {
                    "headline": "Battery policy headline",
                    "signature_text": "battery policy factory approval",
                    "content_fingerprint": "fingerprint-1",
                    "published_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T01:00:00+00:00",
                    "representative_item_key": "https://example.com/news",
                    "active_doc_id": "doc-active-1",
                    "member_item_keys": ["https://example.com/news"],
                    "last_similarity": 0.9,
                    "adjudicated_by_llm": True,
                }
            },
            doc_expires_at={"doc-active-1": "2026-04-03T00:00:00+00:00"},
            consecutive_no_change=2,
            total_ingested_count=3,
        )
    )

    reloaded_registry = JsonSourceRegistry(str(source_file))
    reloaded_store = JsonCrawlStateStore(str(state_file))

    reloaded_source = await reloaded_registry.get_source("news-source")
    reloaded_record = await reloaded_store.get_record("news-source")

    assert reloaded_source is not None
    assert reloaded_source.urls == ["https://example.com/news"]
    assert reloaded_source.source_type == "page"
    assert reloaded_source.schedule_mode == "fixed"
    assert reloaded_source.feed_filter.include_patterns == ["battery"]
    assert reloaded_source.feed_filter.include_authors == ["analyst"]
    assert reloaded_source.feed_filter.exclude_categories == ["gossip"]
    assert reloaded_source.feed_filter.allowed_domains == ["example.com"]
    assert reloaded_source.feed_filter.max_age_days == pytest.approx(7.0)
    assert reloaded_source.feed_retention.mode == "latest"
    assert reloaded_source.feed_retention.max_items == 3
    assert reloaded_source.feed_retention.max_age_days == pytest.approx(30.0)
    assert reloaded_source.feed_priority.mode == "auto"
    assert reloaded_source.feed_priority.priority_patterns == ["earnings"]
    assert reloaded_source.feed_priority.preferred_domains == ["priority.example.com"]
    assert reloaded_source.feed_priority.preferred_authors == ["chief"]
    assert reloaded_source.feed_priority.preferred_categories == ["supply chain"]
    assert reloaded_source.feed_priority.resolved_mode() == "priority_score"
    assert reloaded_source.feed_dedup.mode == "content_signature"
    assert reloaded_source.feed_dedup.signature_token_limit == 48
    assert reloaded_source.feed_dedup.resolved_mode() == "content_signature"
    assert reloaded_source.content_lifecycle.content_class == "short_term_news"
    assert reloaded_source.content_lifecycle.update_mode == "replace_latest"
    assert reloaded_source.content_lifecycle.ttl_days == pytest.approx(2.0)
    assert reloaded_source.content_lifecycle.delete_expired is True
    assert reloaded_source.content_lifecycle.event_cluster_mode == "heuristic_llm"
    assert reloaded_source.content_lifecycle.event_cluster_window_days == pytest.approx(2.5)
    assert reloaded_source.content_lifecycle.event_cluster_min_similarity == pytest.approx(0.81)
    assert reloaded_record is not None
    assert reloaded_record.recent_item_keys == ["https://example.com/news"]
    assert reloaded_record.item_published_at == {
        "https://example.com/news": "2026-04-01T00:00:00+00:00"
    }
    assert reloaded_record.item_content_fingerprints == {
        "https://example.com/news": "fingerprint-1"
    }
    assert reloaded_record.item_active_doc_ids == {
        "https://example.com/news": "doc-active-1"
    }
    assert reloaded_record.item_event_cluster_ids == {
        "https://example.com/news": "cluster-news-1"
    }
    assert reloaded_record.event_clusters["cluster-news-1"].active_doc_id == "doc-active-1"
    assert reloaded_record.event_clusters["cluster-news-1"].adjudicated_by_llm is True
    assert reloaded_record.doc_expires_at == {
        "doc-active-1": "2026-04-03T00:00:00+00:00"
    }
    assert reloaded_record.consecutive_no_change == 2
    assert reloaded_record.total_ingested_count == 3


@pytest.mark.asyncio
async def test_scheduler_only_ingests_changed_pages(tmp_path: Path):
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FakeCrawlerAdapter(
            [
                [_page("same content")],
                [_page("same content")],
                [_page("updated content")],
            ]
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )

    source = await scheduler.add_source(
        MonitoredSource(
            source_id="source-1",
            name="Example",
            urls=["https://example.com/news"],
            interval_seconds=60,
            max_pages=1,
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    second = await scheduler.trigger_now(source.source_id)
    third = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert second["ingested_count"] == 0
    assert second["status"] == "no_change"
    assert third["ingested_count"] == 1
    assert len(rag.calls) == 2
    assert record is not None
    assert record.total_ingested_count == 2


@pytest.mark.asyncio
async def test_sqlite_source_and_state_stores_persist_records(tmp_path: Path):
    db_path = tmp_path / "scheduler.sqlite3"
    registry = SqliteSourceRegistry(str(db_path))
    store = SqliteCrawlStateStore(str(db_path))

    source = MonitoredSource(
        source_id="db-source",
        name="DB Source",
        urls=["https://example.com/feed.xml"],
        interval_seconds=120,
        max_pages=2,
        source_type="feed",
        feed_filter={
            "include_patterns": ["policy"],
            "include_categories": ["industry"],
            "blocked_domains": ["spam.example.com"],
            "max_age_days": 14,
        },
        feed_retention={"mode": "latest", "max_items": 2, "max_age_days": 45},
        feed_priority={
            "mode": "published_desc",
            "priority_patterns": ["policy"],
            "preferred_domains": ["example.com"],
        },
        feed_dedup={"mode": "content_hash"},
        content_lifecycle={
            "content_class": "long_term_knowledge",
            "update_mode": "append",
            "ttl_days": 0,
            "delete_expired": False,
            "event_cluster_mode": "off",
            "event_cluster_window_days": 4,
            "event_cluster_min_similarity": 0.75,
        },
    )
    await registry.upsert_source(source)
    await store.put_record(
        CrawlStateRecord(
            source_id="db-source",
            last_status="success",
            recent_item_keys=["https://example.com/article-1"],
            item_published_at={
                "https://example.com/article-1": "2026-04-01T00:00:00+00:00"
            },
            item_content_fingerprints={
                "https://example.com/article-1": "fingerprint-db-1"
            },
            item_active_doc_ids={"https://example.com/article-1": "doc-db-1"},
            item_event_cluster_ids={
                "https://example.com/article-1": "cluster-db-1"
            },
            event_clusters={
                "cluster-db-1": {
                    "headline": "Policy article",
                    "signature_text": "policy industry update",
                    "content_fingerprint": "fingerprint-db-1",
                    "published_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:30:00+00:00",
                    "representative_item_key": "https://example.com/article-1",
                    "active_doc_id": "doc-db-1",
                    "member_item_keys": ["https://example.com/article-1"],
                }
            },
            doc_expires_at={"doc-db-1": "2026-04-05T00:00:00+00:00"},
            consecutive_no_change=1,
            total_ingested_count=9,
        )
    )

    reloaded_registry = SqliteSourceRegistry(str(db_path))
    reloaded_store = SqliteCrawlStateStore(str(db_path))
    reloaded_source = await reloaded_registry.get_source("db-source")
    reloaded_record = await reloaded_store.get_record("db-source")

    assert reloaded_source is not None
    assert reloaded_source.urls == ["https://example.com/feed.xml"]
    assert reloaded_source.source_type == "feed"
    assert reloaded_source.resolved_schedule_mode() == "adaptive_feed"
    assert reloaded_source.feed_filter.include_patterns == ["policy"]
    assert reloaded_source.feed_filter.include_categories == ["industry"]
    assert reloaded_source.feed_filter.blocked_domains == ["spam.example.com"]
    assert reloaded_source.feed_filter.max_age_days == pytest.approx(14.0)
    assert reloaded_source.feed_retention.mode == "latest"
    assert reloaded_source.feed_retention.max_items == 2
    assert reloaded_source.feed_retention.max_age_days == pytest.approx(45.0)
    assert reloaded_source.feed_priority.mode == "published_desc"
    assert reloaded_source.feed_priority.priority_patterns == ["policy"]
    assert reloaded_source.feed_priority.preferred_domains == ["example.com"]
    assert reloaded_source.feed_priority.resolved_mode() == "published_desc"
    assert reloaded_source.feed_dedup.mode == "content_hash"
    assert reloaded_source.feed_dedup.resolved_mode() == "content_hash"
    assert reloaded_source.content_lifecycle.content_class == "long_term_knowledge"
    assert reloaded_source.content_lifecycle.update_mode == "append"
    assert reloaded_source.content_lifecycle.delete_expired is False
    assert reloaded_source.content_lifecycle.event_cluster_mode == "off"
    assert reloaded_source.content_lifecycle.event_cluster_window_days == pytest.approx(4.0)
    assert reloaded_source.content_lifecycle.event_cluster_min_similarity == pytest.approx(0.75)
    assert reloaded_record is not None
    assert reloaded_record.recent_item_keys == ["https://example.com/article-1"]
    assert reloaded_record.item_published_at == {
        "https://example.com/article-1": "2026-04-01T00:00:00+00:00"
    }
    assert reloaded_record.item_content_fingerprints == {
        "https://example.com/article-1": "fingerprint-db-1"
    }
    assert reloaded_record.item_active_doc_ids == {
        "https://example.com/article-1": "doc-db-1"
    }
    assert reloaded_record.item_event_cluster_ids == {
        "https://example.com/article-1": "cluster-db-1"
    }
    assert reloaded_record.event_clusters["cluster-db-1"].headline == "Policy article"
    assert reloaded_record.doc_expires_at == {
        "doc-db-1": "2026-04-05T00:00:00+00:00"
    }
    assert reloaded_record.consecutive_no_change == 1
    assert reloaded_record.total_ingested_count == 9


@pytest.mark.asyncio
async def test_short_term_news_replace_mode_deletes_superseded_doc(tmp_path: Path):
    rag = _LifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FakeCrawlerAdapter(
            [
                [_page("breaking news initial version")],
                [_page("breaking news updated version with corrections")],
            ]
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="short-term-news",
            name="Short Term News",
            urls=["https://example.com/news"],
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
            },
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert first["deleted_doc_count"] == 0
    assert second["ingested_count"] == 1
    assert second["superseded_count"] == 1
    assert second["deleted_doc_count"] == 1
    assert len(rag.insert_calls) == 2
    assert rag.insert_calls[0]["ids"] != rag.insert_calls[1]["ids"]
    assert rag.delete_calls == [rag.insert_calls[0]["ids"]]
    assert record is not None
    assert record.item_active_doc_ids == {
        "https://example.com/news": rag.insert_calls[1]["ids"]
    }
    assert rag.insert_calls[0]["ids"] not in record.doc_expires_at


@pytest.mark.asyncio
async def test_short_term_feed_event_clustering_supersedes_prior_event_doc(tmp_path: Path):
    feed_first = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Event Cluster Feed</title>
    <item>
      <title>Battery plant approval in Hefei</title>
      <link>https://example.com/article-a</link>
      <pubDate>Tue, 01 Apr 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    feed_second = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Event Cluster Feed</title>
    <item>
      <title>Hefei battery plant approval draws suppliers</title>
      <link>https://example.com/article-b</link>
      <pubDate>Tue, 01 Apr 2026 06:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    rag = _LifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageMetadataCrawlerAdapter(
            {"https://example.com/feed.xml": feed_first},
            {
                "https://example.com/article-a": (
                    "Hefei battery plant approval unlocks local supplier capacity expansion."
                )
            },
            {"https://example.com/article-a": "Battery plant approval in Hefei"},
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-event-cluster",
            name="Feed Event Cluster",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
                "event_cluster_mode": "heuristic",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.68,
            },
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    scheduler.crawler_adapter = _MappedFeedPageMetadataCrawlerAdapter(
        {"https://example.com/feed.xml": feed_second},
        {
            "https://example.com/article-b": (
                "Suppliers react after Hefei battery plant approval expands regional cell output."
            )
        },
        {
            "https://example.com/article-b": "Hefei battery plant approval draws suppliers"
        },
    )
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert second["ingested_count"] == 1
    assert second["superseded_count"] == 1
    assert second["deleted_doc_count"] == 1
    assert rag.delete_calls == [rag.insert_calls[0]["ids"]]
    assert record is not None
    assert record.item_event_cluster_ids["https://example.com/article-a"] == record.item_event_cluster_ids[
        "https://example.com/article-b"
    ]
    cluster_id = record.item_event_cluster_ids["https://example.com/article-b"]
    assert record.event_clusters[cluster_id].active_doc_id == rag.insert_calls[1]["ids"]
    assert set(record.event_clusters[cluster_id].member_item_keys) == {
        "https://example.com/article-a",
        "https://example.com/article-b",
    }


@pytest.mark.asyncio
async def test_short_term_feed_event_clustering_can_use_utility_llm_for_borderline_matches(
    tmp_path: Path,
):
    feed_first = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>LLM Event Cluster Feed</title>
    <item>
      <title>Municipal approval for EV cell project</title>
      <link>https://example.com/article-1</link>
      <pubDate>Tue, 01 Apr 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    feed_second = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
  <channel>
    <title>LLM Event Cluster Feed</title>
    <item>
      <title>City clears EV cell project after municipal approval</title>
      <link>https://example.com/article-2</link>
      <pubDate>Tue, 01 Apr 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    llm = _UtilityEventClusterLLM({"same_event": True, "confidence": 0.93})
    rag = _LifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageMetadataCrawlerAdapter(
            {"https://example.com/feed.xml": feed_first},
            {
                "https://example.com/article-1": (
                    "The municipal government approved a new EV battery cell project in the industrial park."
                )
            },
            {"https://example.com/article-1": "Municipal approval for EV cell project"},
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
        utility_llm_client=llm,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-event-cluster-llm",
            name="Feed Event Cluster LLM",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
                "event_cluster_mode": "heuristic_llm",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.95,
            },
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    scheduler.crawler_adapter = _MappedFeedPageMetadataCrawlerAdapter(
        {"https://example.com/feed.xml": feed_second},
        {
            "https://example.com/article-2": (
                "City regulators cleared the same EV battery cell project in the industrial park after municipal approval."
            )
        },
        {
            "https://example.com/article-2": "City clears EV cell project after municipal approval"
        },
    )
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert second["superseded_count"] == 1
    assert len(llm.calls) == 1
    assert record is not None
    cluster_id = record.item_event_cluster_ids["https://example.com/article-2"]
    assert record.item_event_cluster_ids["https://example.com/article-1"] == cluster_id
    assert record.event_clusters[cluster_id].adjudicated_by_llm is True


@pytest.mark.asyncio
async def test_short_term_feed_event_clustering_can_reuse_global_chunks_vdb_candidates_across_sources(
    tmp_path: Path,
):
    shared_workspace = "ops-news"
    vector_db = _FakeChunksVectorDB()
    rag = _LifecycleRAG(chunks_vdb=vector_db, workspace=shared_workspace)
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageMetadataCrawlerAdapter(
            {"https://publisher-a.example/feed.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Publisher A</title>
    <item>
      <title>Municipal board clears EV cell factory proposal</title>
      <link>https://publisher-a.example/article-1</link>
      <pubDate>Tue, 01 Apr 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""},
            {
                "https://publisher-a.example/article-1": (
                    "The municipal board approved an electric-vehicle cell factory proposal for the port district."
                )
            },
            {
                "https://publisher-a.example/article-1": "Municipal board clears EV cell factory proposal"
            },
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source_a = await scheduler.add_source(
        MonitoredSource(
            source_id="publisher-a",
            name="Publisher A",
            urls=["https://publisher-a.example/feed.xml"],
            workspace=shared_workspace,
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
                "event_cluster_mode": "heuristic",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.68,
            },
        )
    )
    source_b = await scheduler.add_source(
        MonitoredSource(
            source_id="publisher-b",
            name="Publisher B",
            urls=["https://publisher-b.example/feed.xml"],
            workspace=shared_workspace,
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
                "event_cluster_mode": "heuristic",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.68,
            },
        )
    )

    first = await scheduler.trigger_now(source_a.source_id)
    record_a = await scheduler.state_store.get_record(source_a.source_id)
    assert first["ingested_count"] == 1
    assert record_a is not None
    cluster_id = record_a.item_event_cluster_ids["https://publisher-a.example/article-1"]
    first_doc_id = rag.insert_calls[0]["ids"]
    vector_db.set_results(
        [{"full_doc_id": first_doc_id, "distance": 0.96, "content": "cluster anchor"}]
    )
    scheduler.crawler_adapter = _MappedFeedPageMetadataCrawlerAdapter(
        {"https://publisher-b.example/feed.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Publisher B</title>
    <item>
      <title>City council approves battery plant after regulator vote</title>
      <link>https://publisher-b.example/article-2</link>
      <pubDate>Tue, 01 Apr 2026 06:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""},
        {
            "https://publisher-b.example/article-2": (
                "City council approved a battery plant after a regulator vote on the same EV cell project in the port district."
            )
        },
        {
            "https://publisher-b.example/article-2": "City council approves battery plant after regulator vote"
        },
    )

    second = await scheduler.trigger_now(source_b.source_id)
    updated_a = await scheduler.state_store.get_record(source_a.source_id)
    record_b = await scheduler.state_store.get_record(source_b.source_id)

    assert second["ingested_count"] == 1
    assert second["superseded_count"] == 1
    assert len(vector_db.calls) >= 1
    assert updated_a is not None
    assert record_b is not None
    assert record_b.item_event_cluster_ids["https://publisher-b.example/article-2"] == cluster_id
    assert updated_a.event_clusters[cluster_id].active_doc_id == rag.insert_calls[1]["ids"]
    assert first_doc_id in updated_a.doc_expires_at


@pytest.mark.asyncio
async def test_expired_short_term_doc_clears_external_cluster_active_ref_even_without_delete(
    tmp_path: Path,
):
    shared_workspace = "ops-news"
    vector_db = _FakeChunksVectorDB()
    rag = _LifecycleRAG(chunks_vdb=vector_db, workspace=shared_workspace)
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageMetadataCrawlerAdapter(
            {"https://publisher-a.example/feed.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Publisher A</title>
    <item>
      <title>Port battery project cleared</title>
      <link>https://publisher-a.example/article-1</link>
      <pubDate>Tue, 01 Apr 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""},
            {
                "https://publisher-a.example/article-1": (
                    "Officials cleared a battery project for the port district."
                )
            },
            {"https://publisher-a.example/article-1": "Port battery project cleared"},
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source_a = await scheduler.add_source(
        MonitoredSource(
            source_id="publisher-a",
            name="Publisher A",
            urls=["https://publisher-a.example/feed.xml"],
            workspace=shared_workspace,
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
                "event_cluster_mode": "heuristic",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.68,
            },
        )
    )
    source_b = await scheduler.add_source(
        MonitoredSource(
            source_id="publisher-b",
            name="Publisher B",
            urls=["https://publisher-b.example/feed.xml"],
            workspace=shared_workspace,
            interval_seconds=60,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": False,
                "event_cluster_mode": "heuristic",
                "event_cluster_window_days": 7,
                "event_cluster_min_similarity": 0.68,
            },
        )
    )

    await scheduler.trigger_now(source_a.source_id)
    record_a = await scheduler.state_store.get_record(source_a.source_id)
    cluster_id = record_a.item_event_cluster_ids["https://publisher-a.example/article-1"]
    first_doc_id = rag.insert_calls[0]["ids"]
    vector_db.set_results(
        [{"full_doc_id": first_doc_id, "distance": 0.97, "content": "cluster anchor"}]
    )
    scheduler.crawler_adapter = _MappedFeedPageMetadataCrawlerAdapter(
        {"https://publisher-b.example/feed.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Publisher B</title>
    <item>
      <title>Harbor factory approval follows board clearance</title>
      <link>https://publisher-b.example/article-2</link>
      <pubDate>Tue, 01 Apr 2026 03:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""},
        {
            "https://publisher-b.example/article-2": (
                "Harbor officials approved the same battery factory project after the earlier board clearance."
            )
        },
        {
            "https://publisher-b.example/article-2": "Harbor factory approval follows board clearance"
        },
    )
    await scheduler.trigger_now(source_b.source_id)
    record_b = await scheduler.state_store.get_record(source_b.source_id)
    second_doc_id = rag.insert_calls[1]["ids"]
    assert record_b is not None
    await scheduler.state_store.put_record(
        CrawlStateRecord(
            source_id=record_b.source_id,
            last_crawled_at=record_b.last_crawled_at,
            last_content_hashes=dict(record_b.last_content_hashes),
            recent_item_keys=list(record_b.recent_item_keys),
            item_published_at=dict(record_b.item_published_at),
            item_content_fingerprints=dict(record_b.item_content_fingerprints),
            item_active_doc_ids=dict(record_b.item_active_doc_ids),
            item_event_cluster_ids=dict(record_b.item_event_cluster_ids),
            event_clusters=dict(record_b.event_clusters),
            doc_expires_at={
                **dict(record_b.doc_expires_at),
                second_doc_id: (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
            },
            last_status=record_b.last_status,
            consecutive_failures=record_b.consecutive_failures,
            consecutive_no_change=record_b.consecutive_no_change,
            total_ingested_count=record_b.total_ingested_count,
            last_error=record_b.last_error,
        )
    )
    scheduler.crawler_adapter = _MappedFeedPageMetadataCrawlerAdapter(
        {"https://publisher-b.example/feed.xml": """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Publisher B</title>
    <item>
      <title>Harbor factory approval follows board clearance</title>
      <link>https://publisher-b.example/article-2</link>
      <pubDate>Tue, 01 Apr 2026 03:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""},
        {
            "https://publisher-b.example/article-2": (
                "Harbor officials approved the same battery factory project after the earlier board clearance."
            )
        },
        {
            "https://publisher-b.example/article-2": "Harbor factory approval follows board clearance"
        },
    )

    result = await scheduler.trigger_now(source_b.source_id)
    updated_a = await scheduler.state_store.get_record(source_a.source_id)
    updated_b = await scheduler.state_store.get_record(source_b.source_id)

    assert result["expired_doc_count"] >= 1
    assert updated_a is not None
    assert updated_b is not None
    assert updated_a.event_clusters[cluster_id].active_doc_id is None
    assert second_doc_id not in updated_b.item_active_doc_ids.values()


@pytest.mark.asyncio
async def test_scheduler_run_once_sweeps_expired_docs_even_when_source_not_due(
    tmp_path: Path,
):
    class _FailOnCrawlAdapter:
        async def crawl_urls(self, urls, *, max_pages=None):
            raise AssertionError("Expired-doc sweep should not trigger a crawl")

    rag = _LifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FailOnCrawlAdapter(),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="ttl-news",
            name="TTL News",
            urls=["https://example.com/news"],
            interval_seconds=3600,
            max_pages=1,
            content_lifecycle={
                "content_class": "short_term_news",
                "update_mode": "replace_latest",
                "delete_expired": True,
            },
        )
    )
    await scheduler.state_store.put_record(
        CrawlStateRecord(
            source_id=source.source_id,
            last_crawled_at=datetime.now(timezone.utc).isoformat(),
            last_content_hashes={"https://example.com/news": "hash-current"},
            item_active_doc_ids={"https://example.com/news": "doc-expired"},
            doc_expires_at={
                "doc-expired": (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat()
            },
            last_status="success",
        )
    )

    await scheduler._run_once()
    record = await scheduler.state_store.get_record(source.source_id)

    assert rag.delete_calls == ["doc-expired"]
    assert record is not None
    assert record.item_active_doc_ids == {}
    assert record.doc_expires_at == {}


@pytest.mark.asyncio
async def test_remove_short_term_source_deletes_managed_docs_when_supported(
    tmp_path: Path,
):
    rag = _LifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FakeCrawlerAdapter([]),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="removed-news",
            name="Removed News",
            urls=["https://example.com/news"],
            content_lifecycle={"content_class": "short_term_news"},
        )
    )
    await scheduler.state_store.put_record(
        CrawlStateRecord(
            source_id=source.source_id,
            item_active_doc_ids={"https://example.com/news": "doc-active"},
            doc_expires_at={"doc-expired": "2026-04-01T00:00:00+00:00"},
            total_ingested_count=2,
        )
    )

    removed = await scheduler.remove_source(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert removed is True
    assert rag.delete_calls == ["doc-active", "doc-expired"]
    assert record is None


@pytest.mark.asyncio
async def test_remove_short_term_source_keeps_tombstone_when_delete_unsupported(
    tmp_path: Path,
):
    rag = _NoDeleteLifecycleRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FakeCrawlerAdapter([]),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="removed-news-no-delete",
            name="Removed News No Delete",
            urls=["https://example.com/news"],
            content_lifecycle={"content_class": "short_term_news"},
        )
    )
    await scheduler.state_store.put_record(
        CrawlStateRecord(
            source_id=source.source_id,
            item_active_doc_ids={"https://example.com/news": "doc-active"},
            total_ingested_count=1,
        )
    )

    removed = await scheduler.remove_source(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert removed is True
    assert record is not None
    assert record.last_status == "removed"
    assert record.item_active_doc_ids == {}
    assert list(record.doc_expires_at) == ["doc-active"]

    readded = await scheduler.add_source(
        MonitoredSource(
            source_id=source.source_id,
            name="Removed News No Delete",
            urls=["https://example.com/news"],
            content_lifecycle={"content_class": "short_term_news"},
        )
    )
    reset_record = await scheduler.state_store.get_record(readded.source_id)

    assert reset_record is not None
    assert reset_record.last_status == "never_run"
    assert reset_record.doc_expires_at == {}


@pytest.mark.asyncio
async def test_scheduler_shared_coordinator_prevents_duplicate_polling(tmp_path: Path):
    crawler = _BlockingCrawlerAdapter([_page("coordinated content")])
    coordinator = LocalSchedulerCoordinator()
    registry = JsonSourceRegistry(str(tmp_path / "sources.json"))
    state_store = JsonCrawlStateStore(str(tmp_path / "state.json"))
    rag_a = _FakeRAG()
    rag_b = _FakeRAG()
    scheduler_a = IngestScheduler(
        rag_provider=lambda workspace: rag_a,
        crawler_adapter=crawler,
        source_registry=registry,
        state_store=state_store,
        enabled=False,
        coordinator=coordinator,
    )
    scheduler_b = IngestScheduler(
        rag_provider=lambda workspace: rag_b,
        crawler_adapter=crawler,
        source_registry=registry,
        state_store=state_store,
        enabled=False,
        coordinator=coordinator,
    )
    source = await scheduler_a.add_source(
        MonitoredSource(
            source_id="shared-source",
            name="Shared",
            urls=["https://example.com/news"],
            interval_seconds=60,
            max_pages=1,
        )
    )

    first_task = asyncio.create_task(scheduler_a.trigger_now(source.source_id))
    await crawler.started.wait()
    second = await scheduler_b.trigger_now(source.source_id)
    crawler.release.set()
    first = await first_task

    assert second["status"] == "busy"
    assert first["ingested_count"] == 1
    assert crawler.calls == 1
    assert len(rag_a.calls) == 1
    assert len(rag_b.calls) == 0


@pytest.mark.asyncio
async def test_scheduler_leader_election_allows_only_one_loop_owner(tmp_path: Path):
    coordinator = LocalSchedulerCoordinator()
    registry = JsonSourceRegistry(str(tmp_path / "sources.json"))
    state_store = JsonCrawlStateStore(str(tmp_path / "state.json"))
    rag_a = _FakeRAG()
    rag_b = _FakeRAG()
    scheduler_a = IngestScheduler(
        rag_provider=lambda workspace: rag_a,
        crawler_adapter=_FakeCrawlerAdapter([[_page("leader content")]]),
        source_registry=registry,
        state_store=state_store,
        enabled=False,
        coordinator=coordinator,
        enable_leader_election=True,
        loop_lease_key="scheduler:test-loop",
    )
    scheduler_b = IngestScheduler(
        rag_provider=lambda workspace: rag_b,
        crawler_adapter=_FakeCrawlerAdapter([[_page("leader content")]]),
        source_registry=registry,
        state_store=state_store,
        enabled=False,
        coordinator=coordinator,
        enable_leader_election=True,
        loop_lease_key="scheduler:test-loop",
    )
    await scheduler_a.add_source(
        MonitoredSource(
            source_id="loop-source",
            name="Loop",
            urls=["https://example.com/news"],
            interval_seconds=1,
            max_pages=1,
        )
    )

    await asyncio.gather(
        scheduler_a._run_once(),
        scheduler_b._run_once(),
    )

    roles = {scheduler_a._leader_role, scheduler_b._leader_role}
    assert roles == {"leader", "standby"}
    assert len(rag_a.calls) + len(rag_b.calls) == 1
    await scheduler_a.stop()
    await scheduler_b.stop()


@pytest.mark.asyncio
async def test_feed_source_expands_into_article_pages():
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    adapter = _FeedCrawlerAdapter(feed_payload)

    pages = await adapter.crawl_urls(
        ["https://example.com/feed.xml"],
        max_pages=2,
    )

    assert [page.final_url for page in pages] == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]
    assert all(page.success for page in pages)


@pytest.mark.asyncio
async def test_feed_scheduler_batches_article_crawls_via_crawl_urls(tmp_path: Path):
    feed_url = "https://example.com/feed.xml"
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    adapter = _BatchOnlyFeedCrawlerAdapter(
        {feed_url: feed_payload},
        {
            "https://example.com/article-1": "first article content",
            "https://example.com/article-2": "second article content",
        },
    )
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=adapter,
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-batch-crawl",
            name="Feed Batch Crawl",
            urls=[feed_url],
            interval_seconds=60,
            max_pages=2,
        )
    )

    result = await scheduler.trigger_now(source.source_id)

    assert result["feed_discovered_count"] == 2
    assert result["requested_count"] == 2
    assert result["ingested_count"] == 2
    assert len(adapter.batch_calls) == 1
    assert adapter.batch_calls[0]["urls"] == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]
    assert adapter.batch_calls[0]["max_pages"] == 2
    assert len(rag.calls) == 2


@pytest.mark.asyncio
async def test_feed_discovery_parses_published_at():
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First</title>
      <link>https://example.com/article-1</link>
      <pubDate>Tue, 01 Apr 2025 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
      <pubDate>Wed, 02 Apr 2025 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
    adapter = _FeedCrawlerAdapter(feed_payload)

    discovered = await adapter.discover_feed_urls(
        "https://example.com/feed.xml",
        top_k=2,
    )

    assert [item.url for item in discovered] == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]
    assert discovered[0].published_at == "2025-04-01T10:00:00+00:00"
    assert discovered[1].published_at == "2025-04-02T10:00:00+00:00"


@pytest.mark.asyncio
async def test_feed_discovery_canonicalizes_urls_and_deduplicates_aliases():
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>Tracked Alias</title>
      <link>https://EXAMPLE.com:443/article-1/?utm_source=rss#top</link>
    </item>
    <item>
      <title>Duplicate Alias</title>
      <link>https://example.com/article-1?ref=feed</link>
    </item>
    <item>
      <title>Second Article</title>
      <link>https://example.com//article-2//?utm_campaign=launch</link>
    </item>
  </channel>
</rss>
"""
    adapter = _FeedCrawlerAdapter(feed_payload)

    discovered = await adapter.discover_feed_urls(
        "https://example.com/feed.xml",
        top_k=5,
    )

    assert [item.url for item in discovered] == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]


@pytest.mark.asyncio
async def test_feed_discovery_parses_author_and_categories():
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Structured Feed</title>
  <entry>
    <title>Policy update</title>
    <updated>2025-04-02T10:00:00Z</updated>
    <author><name>Jane Analyst</name></author>
    <category term="Industry" />
    <category term="EV" />
    <link href="https://sub.example.com/policy-update" />
  </entry>
</feed>
"""
    adapter = _FeedCrawlerAdapter(feed_payload)

    discovered = await adapter.discover_feed_urls(
        "https://example.com/feed.xml",
        top_k=1,
    )

    assert len(discovered) == 1
    assert discovered[0].author == "Jane Analyst"
    assert discovered[0].categories == ["Industry", "EV"]


@pytest.mark.asyncio
async def test_feed_sources_use_adaptive_schedule_after_unchanged_polls(tmp_path: Path):
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-source",
            name="Feed Source",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=2,
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)
    status = await scheduler.get_status()

    assert first["status"] == "success"
    assert first["ingested_count"] == 2
    assert second["status"] == "no_change"
    assert second["ingested_count"] == 0
    assert record is not None
    assert record.consecutive_no_change == 1
    assert len(rag.calls) == 2
    assert status["sources"][0]["resolved_source_type"] == "feed"
    assert status["sources"][0]["resolved_schedule_mode"] == "adaptive_feed"
    assert status["sources"][0]["resolved_feed_priority_mode"] == "feed_order"
    assert status["sources"][0]["resolved_feed_dedup_mode"] == "content_signature"
    assert status["sources"][0]["resolved_event_cluster_mode"] == "heuristic"
    assert status["sources"][0]["effective_interval_seconds"] == 120
    assert status["sources"][0]["tracked_item_count"] == 2


@pytest.mark.asyncio
async def test_scheduler_status_reports_llm_event_cluster_mode_when_utility_available(
    tmp_path: Path,
):
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: _FakeRAG(),
        crawler_adapter=_FakeCrawlerAdapter([]),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
        utility_llm_client=_UtilityEventClusterLLM({"same_event": True}),
    )
    await scheduler.add_source(
        MonitoredSource(
            source_id="feed-source-llm-status",
            name="Feed Source LLM Status",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
        )
    )

    status = await scheduler.get_status()

    assert status["sources"][0]["resolved_event_cluster_mode"] == "heuristic_llm"


def test_feed_sources_can_opt_out_of_adaptive_schedule():
    source = MonitoredSource(
        source_id="feed-fixed",
        name="Feed Fixed",
        urls=["https://example.com/feed.xml"],
        interval_seconds=60,
        source_type="feed",
        schedule_mode="fixed",
    )
    record = CrawlStateRecord(
        source_id="feed-fixed",
        last_status="no_change",
        consecutive_no_change=3,
    )

    assert IngestScheduler._effective_interval_seconds(source, record) == 60


@pytest.mark.asyncio
async def test_feed_source_filters_entries_before_crawling(tmp_path: Path):
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Filtered Feed</title>
    <item>
      <title>Battery policy update</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>General macro note</title>
      <link>https://example.com/article-2</link>
    </item>
    <item>
      <title>Battery rumor roundup</title>
      <link>https://example.com/article-3</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-filtered",
            name="Filtered Feed",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=3,
            feed_filter={
                "include_patterns": ["battery"],
                "exclude_patterns": ["rumor"],
            },
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["status"] == "success"
    assert result["feed_discovered_count"] == 3
    assert result["feed_filtered_count"] == 2
    assert result["requested_count"] == 1
    assert result["ingested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://example.com/article-1"
    assert rag.calls[0]["metadatas"]["source_label"] == "crawler"
    assert rag.calls[0]["metadatas"]["feed_item_key"] == "https://example.com/article-1"
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/article-1"]
    assert list(record.last_content_hashes.keys()) == ["https://example.com/article-1"]


@pytest.mark.asyncio
async def test_feed_source_filters_by_domain_author_and_category(tmp_path: Path):
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Structured Feed</title>
  <entry>
    <title>Target article</title>
    <updated>2025-04-02T10:00:00Z</updated>
    <author><name>Jane Analyst</name></author>
    <category term="Industry" />
    <link href="https://news.example.com/target-article" />
  </entry>
  <entry>
    <title>Wrong author article</title>
    <updated>2025-04-02T10:00:00Z</updated>
    <author><name>Bob Reporter</name></author>
    <category term="Industry" />
    <link href="https://news.example.com/wrong-author" />
  </entry>
  <entry>
    <title>Blocked domain article</title>
    <updated>2025-04-02T10:00:00Z</updated>
    <author><name>Jane Analyst</name></author>
    <category term="Industry" />
    <link href="https://spam.example.net/blocked-domain" />
  </entry>
  <entry>
    <title>Wrong category article</title>
    <updated>2025-04-02T10:00:00Z</updated>
    <author><name>Jane Analyst</name></author>
    <category term="Macro" />
    <link href="https://news.example.com/wrong-category" />
  </entry>
</feed>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-structured-filter",
            name="Structured Filter",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=5,
            feed_filter={
                "include_authors": ["jane"],
                "include_categories": ["industry"],
                "allowed_domains": ["example.com"],
                "blocked_domains": ["spam.example.net"],
            },
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["feed_discovered_count"] == 4
    assert result["feed_filtered_count"] == 3
    assert result["requested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://news.example.com/target-article"
    assert record is not None
    assert record.recent_item_keys == ["https://news.example.com/target-article"]


@pytest.mark.asyncio
async def test_feed_scheduler_uses_canonical_url_keys_for_no_change_tracking(tmp_path: Path):
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Canonical Feed</title>
    <item>
      <title>Tracked Link</title>
      <link>https://example.com/article-1?utm_source=rss</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FinalUrlVariantFeedCrawlerAdapter(
            feed_payload,
            [
                "https://example.com/article-1/?ref=feed",
                "https://example.com/article-1#section",
            ],
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-canonical-keys",
            name="Feed Canonical Keys",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert second["status"] == "no_change"
    assert second["ingested_count"] == 0
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://example.com/article-1"
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/article-1"]
    assert list(record.last_content_hashes.keys()) == ["https://example.com/article-1"]


@pytest.mark.asyncio
async def test_feed_scheduler_deduplicates_same_content_across_urls(tmp_path: Path):
    feed_payload = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Duplicate Feed</title>
    <item>
      <title>Primary article</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>Mirror article</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageCrawlerAdapter(
            {"https://example.com/feed.xml": feed_payload},
            {
                "https://example.com/article-1": "same body for both links",
                "https://example.com/article-2": "same body for both links",
            },
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-content-dedup",
            name="Feed Content Dedup",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=2,
            feed_dedup={"mode": "content_hash"},
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["feed_discovered_count"] == 2
    assert result["requested_count"] == 2
    assert result["feed_deduplicated_count"] == 1
    assert result["ingested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://example.com/article-1"
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/article-1"]
    assert set(record.item_content_fingerprints.keys()) == {
        "https://example.com/article-1"
    }


@pytest.mark.asyncio
async def test_feed_scheduler_signature_dedup_suppresses_near_duplicate_updates(tmp_path: Path):
    feed_first = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Near Duplicate Feed</title>
    <item>
      <title>Battery update A</title>
      <link>https://example.com/article-a</link>
    </item>
  </channel>
</rss>
"""
    feed_second = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Near Duplicate Feed</title>
    <item>
      <title>Battery update B</title>
      <link>https://example.com/article-b</link>
    </item>
  </channel>
</rss>
"""
    shared_prefix = (
        "battery market update strong demand supply chain expansion margin pressure easing "
    )
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedPageCrawlerAdapter(
            {
                "https://example.com/feed.xml": feed_first,
            },
            {
                "https://example.com/article-a": shared_prefix + "first version adds factory details",
            },
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-signature-dedup",
            name="Feed Signature Dedup",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
            feed_dedup={"mode": "content_signature", "signature_token_limit": 8},
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    scheduler.crawler_adapter = _MappedFeedPageCrawlerAdapter(
        {
            "https://example.com/feed.xml": feed_second,
        },
        {
            "https://example.com/article-b": shared_prefix + "second version adds policy details",
        },
    )
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 1
    assert second["status"] == "no_change"
    assert second["feed_deduplicated_count"] == 1
    assert second["ingested_count"] == 0
    assert len(rag.calls) == 1
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/article-a"]
    assert set(record.item_content_fingerprints.keys()) == {
        "https://example.com/article-a"
    }


@pytest.mark.asyncio
async def test_feed_source_priority_mode_published_desc_prefers_newer_entry(tmp_path: Path):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    feed_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Priority Feed</title>
  <entry>
    <title>Older entry</title>
    <updated>{old}</updated>
    <link href="https://example.com/older-entry" />
  </entry>
  <entry>
    <title>Recent entry</title>
    <updated>{recent}</updated>
    <link href="https://example.com/recent-entry" />
  </entry>
</feed>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-priority-published",
            name="Feed Priority Published",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=1,
            feed_priority={"mode": "published_desc"},
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["feed_discovered_count"] == 2
    assert result["requested_count"] == 1
    assert result["ingested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://example.com/recent-entry"
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/recent-entry"]


@pytest.mark.asyncio
async def test_feed_source_priority_score_ranks_across_multiple_feed_urls(tmp_path: Path):
    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    feed_a_url = "https://example.com/feed-a.xml"
    feed_b_url = "https://example.com/feed-b.xml"
    feed_a = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>General Feed</title>
  <entry>
    <title>General macro note</title>
    <updated>{recent}</updated>
    <author><name>General Reporter</name></author>
    <category term="Macro" />
    <link href="https://news.example.com/general-macro-note" />
  </entry>
</feed>
"""
    feed_b = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Priority Feed</title>
  <entry>
    <title>Battery supply chain deep dive</title>
    <updated>{older}</updated>
    <author><name>Jane Analyst</name></author>
    <category term="Industry" />
    <link href="https://priority.example.com/battery-deep-dive" />
  </entry>
</feed>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_MappedFeedCrawlerAdapter(
            {
                feed_a_url: feed_a,
                feed_b_url: feed_b,
            }
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-priority-score",
            name="Feed Priority Score",
            urls=[feed_a_url, feed_b_url],
            interval_seconds=60,
            max_pages=1,
            feed_priority={
                "mode": "priority_score",
                "priority_patterns": ["battery"],
                "preferred_domains": ["priority.example.com"],
                "preferred_authors": ["jane"],
                "preferred_categories": ["industry"],
            },
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["feed_discovered_count"] == 2
    assert result["requested_count"] == 1
    assert result["ingested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://priority.example.com/battery-deep-dive"
    assert record is not None
    assert record.recent_item_keys == ["https://priority.example.com/battery-deep-dive"]


@pytest.mark.asyncio
async def test_feed_retention_policy_prunes_old_items(tmp_path: Path):
    feed_payload_first = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Retention Feed</title>
    <item>
      <title>First</title>
      <link>https://example.com/article-1</link>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    feed_payload_second = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Retention Feed</title>
    <item>
      <title>Third</title>
      <link>https://example.com/article-3</link>
    </item>
    <item>
      <title>Second</title>
      <link>https://example.com/article-2</link>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_SequentialFeedCrawlerAdapter(
            [feed_payload_first, feed_payload_second]
        ),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-retention",
            name="Retention Feed",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=2,
            feed_retention={"mode": "latest", "max_items": 2},
        )
    )

    first = await scheduler.trigger_now(source.source_id)
    second = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert first["ingested_count"] == 2
    assert second["ingested_count"] == 1
    assert record is not None
    assert record.recent_item_keys == [
        "https://example.com/article-3",
        "https://example.com/article-2",
    ]
    assert set(record.last_content_hashes.keys()) == {
        "https://example.com/article-3",
        "https://example.com/article-2",
    }
    assert "https://example.com/article-1" not in record.last_content_hashes


@pytest.mark.asyncio
async def test_feed_filter_max_age_days_skips_old_entries(tmp_path: Path):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    feed_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Age Filter Feed</title>
  <entry>
    <title>Fresh entry</title>
    <updated>{recent}</updated>
    <link href="https://example.com/fresh-entry" />
  </entry>
  <entry>
    <title>Old entry</title>
    <updated>{old}</updated>
    <link href="https://example.com/old-entry" />
  </entry>
</feed>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-age-filter",
            name="Feed Age Filter",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=5,
            feed_filter={"max_age_days": 7},
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["feed_discovered_count"] == 2
    assert result["feed_filtered_count"] == 1
    assert result["requested_count"] == 1
    assert result["ingested_count"] == 1
    assert len(rag.calls) == 1
    assert rag.calls[0]["file_paths"] == "https://example.com/fresh-entry"
    assert record is not None
    assert record.recent_item_keys == ["https://example.com/fresh-entry"]
    assert record.item_published_at["https://example.com/fresh-entry"] == recent


@pytest.mark.asyncio
async def test_feed_retention_max_age_days_prunes_old_tracked_items(tmp_path: Path):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    feed_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Retention by Age Feed</title>
    <item>
      <title>Fresh entry</title>
      <link>https://example.com/fresh-entry</link>
      <pubDate>{datetime.fromisoformat(recent).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
    </item>
    <item>
      <title>Older entry</title>
      <link>https://example.com/older-entry</link>
      <pubDate>{datetime.fromisoformat(older).strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>
    </item>
  </channel>
</rss>
"""
    rag = _FakeRAG()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=_FeedCrawlerAdapter(feed_payload),
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-age-retention",
            name="Feed Age Retention",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=5,
            feed_retention={"mode": "keep_all", "max_age_days": 7},
        )
    )
    await scheduler.state_store.put_record(
        CrawlStateRecord(
            source_id=source.source_id,
            last_content_hashes={"https://example.com/stale-entry": "hash-stale"},
            recent_item_keys=["https://example.com/stale-entry"],
            item_published_at={"https://example.com/stale-entry": old},
            last_status="success",
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["ingested_count"] == 2
    assert record is not None
    assert "https://example.com/stale-entry" not in record.last_content_hashes
    assert set(record.last_content_hashes.keys()) == {
        "https://example.com/fresh-entry",
    }
    assert set(record.item_published_at.keys()) == {
        "https://example.com/fresh-entry",
    }
