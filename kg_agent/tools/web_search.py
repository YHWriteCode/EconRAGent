from __future__ import annotations

import re
from typing import Any

from kg_agent.crawler.crawler_adapter import CrawledPage
from kg_agent.tools.base import ToolResult
from kg_agent.tools.rerank_utils import (
    get_rerank_candidate_limit,
    get_rerank_settings,
    rerank_payloads,
)


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
    search_query: str | None = None,
    urls: list[str] | None = None,
    top_k: int = 5,
    rag=None,
    crawler_adapter=None,
    **_: Any,
) -> ToolResult:
    effective_query = (search_query or query or "").strip()
    rerank_model_func, min_rerank_score = get_rerank_settings(rag)
    if crawler_adapter is None:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data={
                "status": "not_configured",
                "query": effective_query,
                "top_k": top_k,
                "summary": "Web crawler adapter is not configured yet",
            },
            error="Web crawler adapter is not configured",
            metadata={"implemented": False, "provider": None},
        )

    target_urls = _collect_urls(effective_query, urls)
    if not target_urls:
        discovery_limit = get_rerank_candidate_limit(
            final_limit=top_k,
            rerank_model_func=rerank_model_func,
        )
        discovered = await crawler_adapter.discover_urls(
            effective_query,
            top_k=discovery_limit,
        )
        target_urls = [item.url for item in discovered]
        if not target_urls:
            return ToolResult(
                tool_name="web_search",
                success=False,
                data={
                    "status": "discovery_failed",
                    "query": effective_query,
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
            max_pages=min(discovery_limit, len(target_urls)),
        )
        raw_success_count = sum(1 for page in pages if page.success)
        page_dicts = [_page_to_public_dict(page) for page in pages if page.success]
        page_dicts, rerank_metadata = await rerank_payloads(
            query=effective_query,
            payloads=page_dicts,
            rerank_model_func=rerank_model_func,
            top_n=top_k,
            min_rerank_score=min_rerank_score,
            content_fields=("title", "excerpt", "markdown"),
        )
        returned_count = len(page_dicts)
        status = "success" if returned_count and raw_success_count == len(pages) else "partial_success"
        if returned_count == 0:
            status = "failed"
        return ToolResult(
            tool_name="web_search",
            success=returned_count > 0,
            data={
                "status": status,
                "query": effective_query,
                "urls": target_urls,
                "discovered_results": [item.to_dict() for item in discovered],
                "pages": page_dicts,
                "summary": (
                    f"Discovered {len(discovered)} URLs and crawled "
                    f"{len(pages)} pages, {raw_success_count} crawls succeeded"
                    f" and {returned_count} pages were returned"
                ),
            },
            error=None
            if returned_count > 0
            else "Discovered URLs but all requested pages failed to crawl",
            metadata={
                "implemented": True,
                "provider": "crawl4ai",
                "url_discovery_configured": True,
                "discovered_count": len(discovered),
                "requested_count": len(target_urls),
                "success_count": returned_count,
                "crawl_success_count": raw_success_count,
                **rerank_metadata,
            },
        )

    crawl_limit = get_rerank_candidate_limit(
        final_limit=top_k,
        rerank_model_func=rerank_model_func,
        available_count=len(target_urls),
    )
    pages = await crawler_adapter.crawl_urls(
        target_urls,
        max_pages=crawl_limit,
    )
    raw_success_count = sum(1 for page in pages if page.success)
    page_dicts = [_page_to_public_dict(page) for page in pages if page.success]
    page_dicts, rerank_metadata = await rerank_payloads(
        query=effective_query,
        payloads=page_dicts,
        rerank_model_func=rerank_model_func,
        top_n=top_k,
        min_rerank_score=min_rerank_score,
        content_fields=("title", "excerpt", "markdown"),
    )
    returned_count = len(page_dicts)
    status = "success" if returned_count and raw_success_count == len(pages) else "partial_success"
    if returned_count == 0:
        status = "failed"

    return ToolResult(
        tool_name="web_search",
        success=returned_count > 0,
        data={
            "status": status,
            "query": effective_query,
            "urls": target_urls,
            "discovered_results": [],
            "pages": page_dicts,
            "summary": (
                f"Crawled {len(pages)} pages, {raw_success_count} crawls succeeded"
                f" and {returned_count} pages were returned"
            ),
        },
        error=None
        if returned_count > 0
        else "All requested pages failed to crawl",
        metadata={
            "implemented": True,
            "provider": "crawl4ai",
            "url_discovery_configured": True,
            "discovered_count": 0,
            "requested_count": len(target_urls),
            "success_count": returned_count,
            "crawl_success_count": raw_success_count,
            **rerank_metadata,
        },
    )
