import pytest

from kg_agent.agent.builtin_tools import cross_session_search, memory_search
from kg_agent.tools.web_search import web_search


class _FakeRAG:
    def __init__(self, rerank_model_func=None, min_rerank_score=0.0):
        self.rerank_model_func = rerank_model_func
        self.min_rerank_score = min_rerank_score


class _FakeMemoryStore:
    def __init__(self, matches):
        self.matches = list(matches)
        self.requested_limits: list[int] = []

    async def search(self, session_id: str, query: str, limit: int = 4):
        del session_id, query
        self.requested_limits.append(limit)
        return list(self.matches)


class _FakeCrossSessionStore:
    def __init__(self, matches):
        self.matches = list(matches)
        self.calls: list[dict] = []

    async def search(self, user_id, query, limit=5, exclude_session_id=None):
        self.calls.append(
            {
                "user_id": user_id,
                "query": query,
                "limit": limit,
                "exclude_session_id": exclude_session_id,
            }
        )
        return list(self.matches)


class _FakeCrawlerAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def crawl_urls(self, urls, *, max_pages=None):
        self.calls.append({"urls": list(urls), "max_pages": max_pages})
        return self.pages[: max_pages or len(self.pages)]

    async def discover_urls(self, query, *, top_k=5):
        self.calls.append({"discover_query": query, "top_k": top_k})
        return []


async def _reverse_rerank(*, query, documents, top_n=None, **kwargs):
    del query, kwargs
    ordered = list(range(len(documents) - 1, -1, -1))
    if top_n is not None:
        ordered = ordered[:top_n]
    return [
        {"index": index, "relevance_score": float(len(documents) - position)}
        for position, index in enumerate(ordered)
    ]


@pytest.mark.asyncio
async def test_memory_search_expands_candidates_and_applies_rerank():
    store = _FakeMemoryStore(
        [
            {"content": "battery supply update", "session_id": "session-1"},
            {"content": "supplier contract update", "session_id": "session-1"},
        ]
    )

    result = await memory_search(
        memory_store=store,
        session_id="session-1",
        query="update",
        limit=1,
        rag=_FakeRAG(rerank_model_func=_reverse_rerank),
    )

    assert result.success is True
    assert store.requested_limits == [3]
    assert result.data["matches"][0]["content"] == "supplier contract update"
    assert result.metadata["rerank_applied"] is True


@pytest.mark.asyncio
async def test_cross_session_search_applies_rerank_to_matches():
    store = _FakeCrossSessionStore(
        [
            {"content": "Older logistics note", "session_id": "a"},
            {"content": "Most relevant supplier contract", "session_id": "b"},
        ]
    )

    result = await cross_session_search(
        cross_session_store=store,
        user_id="user-1",
        session_id="session-now",
        query="supplier contract",
        limit=1,
        rag=_FakeRAG(rerank_model_func=_reverse_rerank),
    )

    assert result.success is True
    assert store.calls[0]["limit"] == 3
    assert result.data["matches"][0]["content"] == "Most relevant supplier contract"
    assert result.metadata["rerank_applied"] is True


@pytest.mark.asyncio
async def test_web_search_reranks_direct_url_results():
    pages = [
        type(
            "Page",
            (),
            {
                "url": "https://example.com/a",
                "final_url": "https://example.com/a",
                "success": True,
                "title": "General market overview",
                "excerpt": "overview",
                "markdown": "macro overview",
                "links": [],
                "metadata": {},
                "error": None,
            },
        )(),
        type(
            "Page",
            (),
            {
                "url": "https://example.com/b",
                "final_url": "https://example.com/b",
                "success": True,
                "title": "Supplier contract update",
                "excerpt": "supplier contract",
                "markdown": "supplier contract update",
                "links": [],
                "metadata": {},
                "error": None,
            },
        )(),
    ]
    adapter = _FakeCrawlerAdapter(pages)

    result = await web_search(
        query="supplier contract https://example.com/a https://example.com/b",
        crawler_adapter=adapter,
        top_k=1,
        rag=_FakeRAG(rerank_model_func=_reverse_rerank),
    )

    assert result.success is True
    assert adapter.calls[0]["max_pages"] == 2
    assert len(result.data["pages"]) == 1
    assert result.data["pages"][0]["title"] == "Supplier contract update"
    assert result.metadata["rerank_applied"] is True
