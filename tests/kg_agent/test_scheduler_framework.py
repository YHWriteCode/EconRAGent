from pathlib import Path

import pytest

from kg_agent.crawler.crawl_state_store import CrawlStateRecord, JsonCrawlStateStore
from kg_agent.crawler.crawler_adapter import CrawledPage
from kg_agent.crawler.scheduler import IngestScheduler
from kg_agent.crawler.source_registry import JsonSourceRegistry, MonitoredSource


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
