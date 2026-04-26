# AGENTS.md - kg_agent/crawler

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/crawler/` owns web crawling and recurring ingest support for the agent layer.

It is responsible for:

- Crawl4AI-backed URL crawling and discovery
- Search-result extraction and URL canonicalization
- Source definitions for pages and feeds
- Crawl-state persistence
- Recurring scheduling, source polling, and short-term document lifecycle handling

It is not responsible for:

- Direct agent routing logic
- Native tool registration
- Lower-layer graph storage or retrieval internals

---

## 2. Key Files And Responsibilities

```text
crawler/
|-- crawler_adapter.py     # Crawl4AIAdapter, CrawledPage, DiscoveredUrl
|-- content_extractor.py   # Search-page extraction, scoring, URL cleanup, markdown normalization
|-- source_registry.py     # MonitoredSource plus JSON/SQLite source persistence
|-- crawl_state_store.py   # CrawlStateRecord plus JSON/SQLite crawl-state persistence
`-- scheduler.py           # IngestScheduler, coordination leases, recurring polling, lifecycle management
```

**Important concepts:**

- `MonitoredSource` carries source typing and per-source policy such as feed filtering, retention, priority, dedup, and content lifecycle.
- `CrawlStateRecord` tracks content hashes, retained feed items, active doc IDs, expiry markers, and event-cluster bookkeeping.
- `IngestScheduler` coordinates recurring polling and bridges crawler output into `rag.ainsert()` or deletion/supersession flows when the active backend supports them.
- Scheduler ingest passes crawler provenance through `rag.ainsert(metadatas=...)`; keep `source_label="crawler"` while leaving `file_path` as the URL or feed item key for citation compatibility.

---

## 3. Canonical Behavior

`web_search` and recurring ingest both rely on this layer.

- Direct URL mode crawls user-provided URLs.
- Discovery mode crawls a DuckDuckGo results page, extracts candidate URLs, ranks them, and then crawls the selected pages.
- Feed-aware scheduling adds source typing, canonical URL tracking, retention windows, dedup, and optional short-term news lifecycle behavior.
- Scheduler coordination supports local leases and optional Redis leases so multiple workers do not process the same source loop blindly.

This layer is also where short-term news handling lives:

- append versus replace behavior for tracked items
- optional expiry deletion
- event-cluster reuse across sources in the same workspace
- retrieval suppression for expired or superseded crawler-managed docs

---

## 4. Modification Guidance

- Keep Crawl4AI integration inside `crawler_adapter.py`; do not call Crawl4AI directly from `agent_core.py` or unrelated tool code.
- Keep search-result parsing and URL scoring logic in `content_extractor.py`, not mixed into scheduler or agent code.
- Extend feed/source behavior by evolving `MonitoredSource` policy objects rather than adding ad hoc scheduler-only flags.
- When adding persistence behavior, preserve the existing JSON and SQLite registry/state-store patterns.
- Maintain canonical URL normalization rules consistently across discovery, dedup, retention, and no-change detection paths.

---

## 5. Known Limitations And TODOs

- Scheduler coordination supports local leases and optional leader election, but there is still no scheduler sharding or richer distributed control plane.
- Source persistence currently supports `json` and `sqlite`; there is still no Redis, MongoDB, or Postgres source/state backend.
- RSS and feed support is now fairly broad, but source management is still URL-centric and does not yet model richer source credibility or feed provenance.
- Update-aware provenance is still partial. The scheduler tags crawler chunks with source/feed/event metadata and can supersede or delete short-term documents, but it does not maintain full version-aware histories or semantic delta ingest.
- Similar-news consolidation is heuristic and workspace-local. It is not a full long-lived event graph.
- Retrieval suppression for expired or superseded crawler docs is best effort and still depends on lower-layer metadata such as `chunk_id`, `source_id`, and reverse mapping through `rag.text_chunks`.
- Removed-source tombstones are a pragmatic fallback for backends that cannot delete by document ID; they are not a full archival model.
