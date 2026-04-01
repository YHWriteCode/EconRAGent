from __future__ import annotations

import re
from typing import Any

from kg_agent.crawler.crawler_adapter import CrawledPage
from kg_agent.tools.base import ToolResult


URL_PATTERN = re.compile(r"https?://[^\s)>\"']+", re.IGNORECASE)


def _collect_urls(query: str, urls: list[str] | None) -> list[str]:
    candidates = list(urls or [])
    candidates.extend(URL_PATTERN.findall(query or ""))
    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        url = (item or "").strip().rstrip(".,;")
        if not url or url in seen:
            continue
        normalized.append(url)
        seen.add(url)
    return normalized


def _page_to_public_dict(page: CrawledPage) -> dict[str, Any]:
    return {
        "url": page.url,
        "final_url": page.final_url,
        "success": page.success,
        "title": page.title,
        "excerpt": page.excerpt,
        "markdown": page.markdown,
        "links": page.links,
        "metadata": page.metadata,
        "error": page.error,
    }


async def web_search(
    *,
    query: str,
    urls: list[str] | None = None,
    top_k: int = 5,
    crawler_adapter=None,
    **_: Any,
) -> ToolResult:
    if crawler_adapter is None:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data={
                "status": "not_configured",
                "query": query,
                "top_k": top_k,
                "summary": "Web crawler adapter is not configured yet",
            },
            error="Web crawler adapter is not configured",
            metadata={"implemented": False, "provider": None},
        )

    target_urls = _collect_urls(query, urls)
    if not target_urls:
        discovered = await crawler_adapter.discover_urls(query, top_k=top_k)
        target_urls = [item.url for item in discovered]
        if not target_urls:
            return ToolResult(
                tool_name="web_search",
                success=False,
                data={
                    "status": "discovery_failed",
                    "query": query,
                    "top_k": top_k,
                    "discovered_results": [],
                    "summary": "Search-result discovery did not return any crawlable URLs",
                },
                error="URL discovery failed for the given natural-language query",
                metadata={
                    "implemented": True,
                    "provider": "crawl4ai",
                    "url_discovery_configured": True,
                    "discovered_count": 0,
                },
            )

        pages = await crawler_adapter.crawl_urls(
            target_urls,
            max_pages=min(top_k, len(target_urls)),
        )
        success_count = sum(1 for page in pages if page.success)
        status = "success" if success_count == len(pages) else "partial_success"
        if success_count == 0:
            status = "failed"
        return ToolResult(
            tool_name="web_search",
            success=success_count > 0,
            data={
                "status": status,
                "query": query,
                "urls": target_urls,
                "discovered_results": [item.to_dict() for item in discovered],
                "pages": [_page_to_public_dict(page) for page in pages],
                "summary": (
                    f"Discovered {len(discovered)} URLs and crawled "
                    f"{len(pages)} pages, {success_count} succeeded"
                ),
            },
            error=None
            if success_count > 0
            else "Discovered URLs but all requested pages failed to crawl",
            metadata={
                "implemented": True,
                "provider": "crawl4ai",
                "url_discovery_configured": True,
                "discovered_count": len(discovered),
                "requested_count": len(target_urls),
                "success_count": success_count,
            },
        )

    pages = await crawler_adapter.crawl_urls(
        target_urls,
        max_pages=min(top_k, len(target_urls)),
    )
    success_count = sum(1 for page in pages if page.success)
    status = "success" if success_count == len(pages) else "partial_success"
    if success_count == 0:
        status = "failed"

    return ToolResult(
        tool_name="web_search",
        success=success_count > 0,
        data={
            "status": status,
            "query": query,
            "urls": target_urls,
            "discovered_results": [],
            "pages": [_page_to_public_dict(page) for page in pages],
            "summary": f"Crawled {len(pages)} pages, {success_count} succeeded",
        },
        error=None
        if success_count > 0
        else "All requested pages failed to crawl",
        metadata={
            "implemented": True,
            "provider": "crawl4ai",
            "url_discovery_configured": True,
            "discovered_count": 0,
            "requested_count": len(target_urls),
            "success_count": success_count,
        },
    )
