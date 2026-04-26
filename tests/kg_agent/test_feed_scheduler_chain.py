from pathlib import Path

import pytest

from kg_agent.crawler.crawl_state_store import JsonCrawlStateStore
from kg_agent.crawler.crawler_adapter import CrawledPage, DiscoveredUrl
from kg_agent.crawler.scheduler import IngestScheduler
from kg_agent.crawler.source_registry import JsonSourceRegistry, MonitoredSource


class _CapturingRAG:
    workspace = "feed-chain-test"

    def __init__(self):
        self.insert_calls: list[dict] = []

    async def ainsert(self, input, file_paths=None, metadatas=None):
        self.insert_calls.append(
            {"input": input, "file_paths": file_paths, "metadatas": metadatas}
        )
        return f"track-{len(self.insert_calls)}"


class _FeedChainCrawlerAdapter:
    def __init__(self):
        self.discover_calls: list[dict] = []
        self.batch_calls: list[dict] = []

    async def discover_feed_urls(self, feed_url: str, *, top_k: int = 5):
        self.discover_calls.append({"feed_url": feed_url, "top_k": top_k})
        return [
            DiscoveredUrl(
                title="Battery policy update",
                url="https://example.com/article-1?utm_source=rss",
                source="rss",
            ),
            DiscoveredUrl(
                title="Supply chain follow-up",
                url="https://example.com/article-2#section",
                source="rss",
            ),
        ][:top_k]

    async def crawl_urls(
        self,
        urls: list[str],
        *,
        max_pages: int | None = None,
        max_content_chars: int | None = None,
    ):
        self.batch_calls.append(
            {
                "urls": list(urls),
                "max_pages": max_pages,
                "max_content_chars": max_content_chars,
            }
        )
        limited_urls = list(urls[:max_pages]) if max_pages is not None else list(urls)
        pages: list[CrawledPage] = []
        for url in limited_urls:
            markdown = (
                "Battery policy support helps manufacturers expand capacity."
                if url.endswith("article-1")
                else "Supply chain localization reduces costs for battery makers."
            )
            pages.append(
                CrawledPage(
                    url=url,
                    final_url=url,
                    success=True,
                    title=f"title for {url.rsplit('/', 1)[-1]}",
                    markdown=markdown,
                    excerpt=markdown[:60],
                )
            )
        return pages


@pytest.mark.asyncio
async def test_feed_scheduler_chain_discovers_crawls_and_ingests(tmp_path: Path):
    rag = _CapturingRAG()
    crawler = _FeedChainCrawlerAdapter()
    scheduler = IngestScheduler(
        rag_provider=lambda workspace: rag,
        crawler_adapter=crawler,
        source_registry=JsonSourceRegistry(str(tmp_path / "sources.json")),
        state_store=JsonCrawlStateStore(str(tmp_path / "state.json")),
        enabled=False,
    )
    source = await scheduler.add_source(
        MonitoredSource(
            source_id="feed-chain",
            name="Feed Chain",
            urls=["https://example.com/feed.xml"],
            interval_seconds=60,
            max_pages=2,
            source_type="feed",
            feed_dedup={"mode": "off"},
        )
    )

    result = await scheduler.trigger_now(source.source_id)
    record = await scheduler.state_store.get_record(source.source_id)

    assert result["status"] == "success"
    assert result["feed_discovered_count"] == 2
    assert result["requested_count"] == 2
    assert result["ingested_count"] == 2
    assert crawler.discover_calls == [
        {"feed_url": "https://example.com/feed.xml", "top_k": 10}
    ]
    assert crawler.batch_calls == [
        {
            "urls": [
                "https://example.com/article-1",
                "https://example.com/article-2",
            ],
            "max_pages": 2,
            "max_content_chars": None,
        }
    ]
    assert len(rag.insert_calls) == 2
    assert rag.insert_calls[0]["file_paths"] == "https://example.com/article-1"
    assert rag.insert_calls[0]["metadatas"]["source_label"] == "crawler"
    assert rag.insert_calls[0]["input"].startswith("Battery policy support")
    assert rag.insert_calls[1]["file_paths"] == "https://example.com/article-2"
    assert record is not None
    assert record.recent_item_keys == [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]
