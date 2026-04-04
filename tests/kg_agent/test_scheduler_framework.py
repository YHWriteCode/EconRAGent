import asyncio
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
    )
    await registry.upsert_source(source)
    await store.put_record(
        CrawlStateRecord(
            source_id="news-source",
            last_status="success",
            total_ingested_count=3,
        )
    )

    reloaded_registry = JsonSourceRegistry(str(source_file))
    reloaded_store = JsonCrawlStateStore(str(state_file))

    reloaded_source = await reloaded_registry.get_source("news-source")
    reloaded_record = await reloaded_store.get_record("news-source")

    assert reloaded_source is not None
    assert reloaded_source.urls == ["https://example.com/news"]
    assert reloaded_record is not None
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
        urls=["https://example.com/db"],
        interval_seconds=120,
        max_pages=2,
    )
    await registry.upsert_source(source)
    await store.put_record(
        CrawlStateRecord(
            source_id="db-source",
            last_status="success",
            total_ingested_count=9,
        )
    )

    reloaded_registry = SqliteSourceRegistry(str(db_path))
    reloaded_store = SqliteCrawlStateStore(str(db_path))
    reloaded_source = await reloaded_registry.get_source("db-source")
    reloaded_record = await reloaded_store.get_record("db-source")

    assert reloaded_source is not None
    assert reloaded_source.urls == ["https://example.com/db"]
    assert reloaded_record is not None
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
