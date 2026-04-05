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

    async def ainsert(self, input, file_paths=None):
        self.calls.append({"input": input, "file_paths": file_paths})
        return f"track-{len(self.calls)}"


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
        return _page(f"content for {url}", url=url)


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


def _page(markdown: str, *, url: str = "https://example.com/news") -> CrawledPage:
    return CrawledPage(
        url=url,
        final_url=url,
        success=True,
        title="News",
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
    )
    await registry.upsert_source(source)
    await store.put_record(
        CrawlStateRecord(
            source_id="news-source",
            last_status="success",
            recent_item_keys=["https://example.com/news"],
            item_published_at={"https://example.com/news": "2026-04-01T00:00:00+00:00"},
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
    assert reloaded_record is not None
    assert reloaded_record.recent_item_keys == ["https://example.com/news"]
    assert reloaded_record.item_published_at == {
        "https://example.com/news": "2026-04-01T00:00:00+00:00"
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
    assert reloaded_record is not None
    assert reloaded_record.recent_item_keys == ["https://example.com/article-1"]
    assert reloaded_record.item_published_at == {
        "https://example.com/article-1": "2026-04-01T00:00:00+00:00"
    }
    assert reloaded_record.consecutive_no_change == 1
    assert reloaded_record.total_ingested_count == 9


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
    assert status["sources"][0]["effective_interval_seconds"] == 120
    assert status["sources"][0]["tracked_item_count"] == 2


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
