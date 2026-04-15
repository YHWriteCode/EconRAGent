# AGENTS.md - kg_agent/tools

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/tools/` owns the native built-in tool implementations used by the agent layer.

It is responsible for:

- The native tool contract and result payload types
- Retrieval, graph, ingest, and web-search tool implementations
- Shared rerank helpers used by tool flows

It is not responsible for:

- Planner logic or route selection
- Capability metadata registration
- Crawl adapter internals
- Skill runtime execution

---

## 2. Key Files And Responsibilities

```text
tools/
|-- base.py              # ToolDefinition and ToolResult
|-- retrieval_tools.py   # kg_hybrid_search, kg_naive_search, freshness/lifecycle filtering helpers
|-- graph_tools.py       # graph_entity_lookup, graph_relation_trace
|-- kg_ingest.py         # Generic text/markdown ingest into LightRAG
|-- web_search.py        # Direct URL crawling and search-result discovery entrypoint
`-- rerank_utils.py      # Shared rerank helpers for tool payloads
```

**Current native tool surface:**

- retrieval tools for KG and naive search
- graph lookup and relation tracing
- knowledge-graph ingest
- web search backed by the crawler layer

Tool handlers are expected to fit the native async tool pattern and return `ToolResult` payloads that remain compact enough for planner and memory use.

---

## 3. Canonical Behavior

- Native tools are execution units. They are registered into `ToolRegistry` and then mirrored into `CapabilityRegistry` when planner-visible exposure is needed.
- `web_search` is only the tool entrypoint. Crawling, discovery, and scheduling details belong in `kg_agent/crawler/`.
- Retrieval tools are responsible for compatibility behavior such as freshness decay fallback and crawler lifecycle filtering when lower-layer support is incomplete or mixed-version.
- `kg_ingest` is the generic bridge from arbitrary text or crawled content into `rag.ainsert()`.

---

## 4. Modification Guidance

- Add or change native tools here, then wire them through `agent/tool_schemas.py`, `agent/builtin_tools.py`, and capability mirroring as needed.
- Do not place domain-specific placeholder business tools in this layer; specialized external systems should usually live behind MCP or the skill runtime.
- Keep tool outputs structured and concise. Conversation memory should not persist full crawled markdown or oversized tool payloads.
- Keep Crawl4AI calls behind the crawler adapter rather than embedding direct crawler logic inside tool handlers.

---

## 5. Known Limitations And TODOs

- `web_search` still depends on the crawler adapter being configured; without it the tool returns `status="not_configured"`.
- Retrieval freshness behavior now has lower-layer support, but compatibility fallback logic still exists in the tool layer for mixed deployments.
- Crawler lifecycle filtering in retrieval is best effort when lower-layer metadata is missing or inconsistent.
