import pytest

from kg_agent.crawler.crawler_adapter import CrawledPage
from kg_agent.tools.web_search import web_search


class _FakeCrawlerAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.discovered = []

    async def crawl_urls(self, urls, *, max_pages=None):
        self.calls.append({"urls": list(urls), "max_pages": max_pages})
        return self.pages

    async def discover_urls(self, query, *, top_k=5):
        self.calls.append({"discover_query": query, "top_k": top_k})
        return self.discovered


@pytest.mark.asyncio
async def test_web_search_crawls_direct_urls_successfully():
    adapter = _FakeCrawlerAdapter(
        [
            CrawledPage(
                url="https://example.com/a",
                success=True,
                title="Example A",
                markdown="BYD expands battery production.",
                excerpt="BYD expands battery production.",
            )
        ]
    )

    result = await web_search(
        query="请抓取这个网页 https://example.com/a",
        crawler_adapter=adapter,
        top_k=3,
    )

    assert result.success is True
    assert result.data["status"] == "success"
    assert result.data["pages"][0]["title"] == "Example A"
    assert adapter.calls[0]["urls"] == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_web_search_returns_clear_error_without_direct_url():
    adapter = _FakeCrawlerAdapter([])

    result = await web_search(
        query="今天油价为什么涨了？",
        crawler_adapter=adapter,
    )

    assert result.success is False
    assert result.data["status"] == "discovery_failed"
    assert "URL discovery failed" in result.error


@pytest.mark.asyncio
async def test_web_search_discovers_urls_from_natural_language_query():
    adapter = _FakeCrawlerAdapter(
        [
            CrawledPage(
                url="https://example.com/discovered",
                success=True,
                title="Discovered Page",
                markdown="Policy support helps BYD expand.",
                excerpt="Policy support helps BYD expand.",
            )
        ]
    )
    adapter.discovered = [
        type(
            "Discovered",
            (),
            {
                "url": "https://example.com/discovered",
                "title": "Discovered Page",
                "source": "duckduckgo",
                "to_dict": lambda self: {
                    "url": self.url,
                    "title": self.title,
                    "source": self.source,
                },
            },
        )()
    ]

    result = await web_search(
        query="How do new energy vehicle policies affect BYD?",
        crawler_adapter=adapter,
        top_k=3,
    )

    assert result.success is True
    assert result.data["status"] == "success"
    assert result.data["discovered_results"][0]["url"] == "https://example.com/discovered"
    assert adapter.calls[0]["discover_query"] == "How do new energy vehicle policies affect BYD?"
    assert adapter.calls[1]["urls"] == ["https://example.com/discovered"]


@pytest.mark.asyncio
async def test_web_search_reports_missing_crawler_adapter():
    result = await web_search(query="https://example.com/a", crawler_adapter=None)

    assert result.success is False
    assert result.data["status"] == "not_configured"
    assert "crawler adapter" in result.error.lower()
