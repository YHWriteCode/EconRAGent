# AGENTS.md — kg_agent

> This file is for AI coding assistants (Copilot / Cursor / Cline, etc.) to reference when modifying code in `kg_agent/`.
> Human developers can also use it as an architecture quick-reference.

---

## 1. Module Positioning

`kg_agent/` is the **business layer** of this project, building an agent system on top of `lightrag_fork/` (graph-vector backend).

**Core Responsibilities:**

- Agent main loop (reasoning → routing → tool invocation → summary response)
- Route Judge: decides "how to query, what to query, how many steps"
- Path Explainer: organizes graph paths + text evidence into readable explanations
- Tool registration and execution (retrieval, graph, memory, web search, quantitative analysis, etc.)
- Conversation memory (dynamic attention window + cross-session + user profile)
- External API service

**Not Responsible For:**

- Document chunking, entity extraction, graph merging, vector writes → handled by `lightrag_fork/`
- Storage backend management (Neo4j / Qdrant / MongoDB, etc.) → handled by `lightrag_fork/kg/`
- LLM low-level adaptation (model call abstraction) → provided by `lightrag_fork/llm/`, wrapped by `AgentLLMClient` in this layer

**Dependency Direction (strictly one-way):**

```
kg_agent/ ──depends on──> lightrag_fork/
lightrag_fork/ ──must not depend on──> kg_agent/
```

---

## 2. Directory Structure and File Responsibilities

```
kg_agent/
├── __init__.py                    # Package entry, exports KGAgentConfig
├── config.py                      # Unified config: models, tool switches, runtime parameters
│
├── agent/                         # Agent core (reasoning + decision + execution)
│   ├── __init__.py
│   ├── agent_core.py              # Main Agent loop: AgentCore.chat() entry point
│   ├── route_judge.py             # Route Judge: RouteJudge.plan() → RouteDecision
│   ├── path_explainer.py          # Path explanation layer: PathExplainer.explain() → PathExplanation
│   ├── tool_registry.py           # Tool registration protocol: ToolRegistry (register, find, execute)
│   ├── builtin_tools.py           # Assembly layer: wraps functions from tools/ and registers them to ToolRegistry
│   ├── prompts.py                 # All prompt templates (route judge version registry / path explainer / final answer)
│   └── tool_schemas.py            # Tool parameter JSON Schema definitions
│
├── tools/                         # General tool layer (one file per tool)
│   ├── __init__.py
│   ├── base.py                    # ToolDefinition + ToolResult data structures
│   ├── retrieval_tools.py         # kg_hybrid_search, kg_naive_search
│   ├── graph_tools.py             # graph_entity_lookup, graph_relation_trace
│   ├── kg_ingest.py               # kg_ingest: insert any text/markdown into KG via rag.ainsert()
│   ├── web_search.py              # web_search: URL crawling + search-engine URL discovery via Crawl4AI
│   └── quant_tools.py             # quant_backtest (placeholder, quant as tool rather than standalone module)
│
├── memory/                        # Memory system
│   ├── __init__.py
│   ├── conversation_memory.py     # ConversationMemoryStore: in-session message storage/search with memory/SQLite/Mongo backends
│   ├── cross_session_store.py     # CrossSessionStore: same-user retrieval across prior sessions
│   └── user_profile.py            # UserProfileStore: user profile storage with memory/SQLite/Mongo backends
│
├── crawler/                       # Web crawling layer
│   ├── __init__.py                # Exports Crawl4AIAdapter, CrawledPage, DiscoveredUrl, IngestScheduler, source/state types
│   ├── crawler_adapter.py         # Crawl4AIAdapter: start/close/crawl_url/crawl_urls/discover_urls; feed parsing also extracts published_at from RSS/Atom entries
│   ├── content_extractor.py       # Search-result extraction, URL scoring/ranking, Markdown normalization, DuckDuckGo redirect decoding
│   ├── source_registry.py         # MonitoredSource (page/feed typing + feed filter/retention policy), SourceRegistry, Json/SQLite source persistence
│   ├── crawl_state_store.py       # CrawlStateRecord (hashes + recent feed item history + item published_at map + failure/no-change streaks), CrawlStateStore, Json/SQLite state persistence
│   └── scheduler.py               # IngestScheduler: recurring crawl + content-hash dedup + feed-aware adaptive scheduling/filtering/retention
│
└── api/                           # External API
    ├── __init__.py
    ├── app.py                     # FastAPI app factory: create_app(), build_rag_from_env(), EnvLightRAGProvider, LightRAG lifecycle management
    └── agent_routes.py            # API route definitions
```

---

## 3. Architecture Constraints (Must Follow)

### 3.1 Business Logic Belongs Only in kg_agent/

- All Agent orchestration, tool invocation, conversation memory, and user profile logic must be written in `kg_agent/`
- Do not add any business-layer code to `lightrag_fork/`
- Do not import any `kg_agent` module from `lightrag_fork/`

### 3.2 Agent Sub-modules Are Not Independent Chat Endpoints

- **RouteJudge** is an internal "pre-decision sub-module" of the main Agent, not exposed as an independent chat endpoint
- **PathExplainer** is an "explanation augmentation module" triggered on-demand by the route judge, not an independent reasoning engine
- The only conversation entry point is `AgentCore.chat()`

### 3.3 No LangChain

- Keep it lightweight, self-built, and interpretable
- Agent loop, tool invocation, and prompt assembly are all self-built
- LLM calls go through `AgentLLMClient` (wrapping OpenAI AsyncClient)
- `AgentCore` can attach a dedicated lightweight utility model for internal routing/explanation work; when it is not configured, Route Judge and Path Explainer warn once and fall back to the main agent model

### 3.4 Tools Are Managed Through ToolRegistry

- All tools must be registered via `ToolRegistry.register()` before use
- Do not hardcode tool invocations in `agent_core.py`
- Quantitative analysis is a tool type (`quant_tools.py`), no longer maintaining a separate top-level directory

### 3.5 Path Explanation Allows Fallback

- When graph paths are insufficient or text evidence is lacking, automatically falls back to a plain answer
- Path explanation is not forced on every response

### 3.6 Reserved Parameters Cannot Be Overridden by Tool Args

`_build_tool_execution_kwargs()` in `agent_core.py` protects the following framework-reserved fields, preventing accidental override by `tool_plan.args` generated by RouteJudge:

- `query`, `rag`, `session_id`, `user_id`
- `session_context`, `user_profile`, `domain_schema`, `memory_store`

These fields are injected uniformly by the framework; a tool handler's `args` can only provide non-reserved parameters.

### 3.7 Web Crawling Goes Through the Crawler Adapter

- Crawl4AI integration belongs to `kg_agent/crawler/`, not `lightrag_fork/`
- Do not call Crawl4AI directly from `agent_core.py`; pass crawling through `crawler_adapter`
- `web_search` supports two modes:
  1. **Direct URL crawling:** when the query or `urls` arg contains explicit URLs, crawls them directly
  2. **Search-engine URL discovery:** when no explicit URLs are provided, uses `crawler_adapter.discover_urls()` to crawl DuckDuckGo search results, extract and rank candidate URLs, then crawls the top results
- URL discovery pipeline: DuckDuckGo HTML search → crawl search page → `content_extractor.extract_search_results_from_markdown()` → score/rank/filter → crawl top-k result pages
- If the crawler adapter is not configured (`crawler_adapter=None`), `web_search` returns `status="not_configured"`

### 3.8 Dynamic-Graph v1 Constraints

- Generic tool chaining / output piping is now supported in `AgentCore` through `ToolCallPlan.input_bindings`
- `web_search -> kg_ingest` can run either as an explicit dynamic-graph bridge or as a normal chained tool sequence with input bindings
- `recent_tool_calls` should only inject compact tool history from the most recent assistant turn unless a later design explicitly changes that rule
- `compact_tool_calls` must remain lightweight: store `tool`, `success`, `summary`, `strategy`, `timestamp`; do not persist full tool data or crawled page markdown in conversation memory
- Scheduler coordination now supports local in-process leases and optional Redis-backed leases for multi-worker / multi-instance polling coordination
- Scheduler also supports optional loop leader election so one scheduler instance can own recurring due-source scans for a shared `loop_lease_key`
- `MonitoredSource` now supports `source_type` (`auto` / `page` / `feed`) and `schedule_mode` (`auto` / `fixed` / `adaptive_feed`) so feed-like URLs can opt into different polling behavior without changing the generic scheduler API
- Feed-aware source policies now live on `MonitoredSource`: `feed_filter` (title/url substring filters plus author/category/domain constraints) and `feed_retention` (`keep_all` or `latest` with `max_items`) are enforced by the scheduler rather than by the generic `web_search` tool path
- `MonitoredSource.feed_priority` now controls feed item ranking before crawl: `mode="published_desc"` prefers newer entries, while `mode="priority_score"` ranks entries by configured title/url pattern hits plus preferred domain/author/category matches and then falls back to `published_at`
- Feed-aware source policies also support publication-time windows: `feed_filter.max_age_days` skips stale entries before crawl, while `feed_retention.max_age_days` prunes old tracked feed items from scheduler state when entry timestamps are available
- `sources_file=""` means in-memory source registry only; non-empty file paths must persist source CRUD changes to JSON

---

## 4. Dynamic Workspace and RAG Provider

### 4.1 Runtime Modes

`AgentCore` supports two ways to obtain a LightRAG instance:

| Mode | Constructor | Use Case |
|---|---|---|
| Fixed rag | `AgentCore(rag=rag_instance)` | Testing, single workspace |
| Dynamic provider | `AgentCore(rag_provider=provider.get)` | **API layer default**, multi-workspace |

**Recommended runtime mode:** The API layer defaults to `rag_provider`, no longer binding to a single fixed rag.

### 4.2 EnvLightRAGProvider

**File:** `api/app.py`

```python
class EnvLightRAGProvider:
    async def get(workspace: str | None) -> LightRAG   # Lazy-load per request workspace
    def list_active_workspaces() -> list[str]           # Currently cached workspace list
    async def finalize_all() -> None                    # Release all on app shutdown
```

**Behavior:**

- `build_rag_from_env()` reads the project root `.env` and bootstraps a `LightRAG` instance for the requested workspace
- On first request for a workspace, calls `build_rag_from_env()` to create a LightRAG instance and `initialize_storages()`
- Subsequent requests for the same workspace reuse the cached instance (`dict[str, LightRAG]`)
- If no workspace is provided in the request, uses `config.runtime.default_workspace`
- On app shutdown (lifespan), calls `finalize_all()` to release all instances

### 4.3 app.state Reference

`create_app()` sets the following on `app.state`:

| Field | Type | Description |
|---|---|---|
| `app.state.rag` | `LightRAG \| None` | Has value in fixed rag mode; `None` in provider mode |
| `app.state.rag_provider` | `EnvLightRAGProvider \| None` | Has value in provider mode; `None` in fixed rag mode |
| `app.state.agent_core` | `AgentCore` | Always present |

**Note:** In provider mode, `app.state.rag` is `None`; the actual working reference is `app.state.rag_provider`.

---

## 5. Main Execution Flow

The complete flow of `AgentCore.chat()` is as follows (step 0 is new for provider mode):

```
0. Resolve LightRAG instance
   └─ Fixed rag mode: use self._rag directly
   └─ Provider mode: await self._rag_provider(workspace) → lazy-load + cache reuse

1. Load context
   ├─ Session history (ConversationMemoryStore.get_context_window: recent-turn anchor + query-aware older-turn backfill within token budget)
   ├─ User profile (UserProfileStore.get_profile)
   └─ Available tool list (enabled tools from ToolRegistry)

2. Invoke Route Judge
   └─ RouteJudge.plan(query, session_context, user_profile, available_tools)
       ├─ Rule engine priority (regex matching for 7 scenario types)
       └─ Optional LLM refinement (when llm_client is available, LLM can adjust rule results)
       → Returns RouteDecision (structured routing plan)

3. Execute tools sequentially per tool_sequence
   └─ ToolRegistry.execute(tool_name, **kwargs)
       ├─ Collect tool results
       ├─ Accumulate graph_paths and evidence_chunks (for path explanation)
       └─ Support early termination (optional tools skipped when preceding tools succeed)

4. Dynamic-graph bridge (conditional)
   └─ For selected strategies only:
       ├─ `freshness_aware_search`
       │    ├─ inspect KG result freshness / gap
       │    └─ optional `web_search.pages -> kg_ingest(content/source)` auto-ingest bridge
       └─ `correction_and_refresh`
            ├─ derive correction search query from recent context
            └─ `web_search.pages -> kg_ingest(content/source)` refresh bridge

5. Path explanation (optional)
   └─ When route.need_path_explanation=True
       PathExplainer.explain(query, graph_paths, evidence_chunks)
       ├─ Candidate path scoring (token overlap + evidence coverage)
       ├─ Select best path + match evidence
       ├─ LLM generates explanation (optional, with fallback)
       └─ On no results, returns enabled=False with empty structure

6. Final answer generation
   └─ LLM summary: query + route + tool_results + path_explanation + history
       └─ Fallback: structured text concatenation

7. Persistence
   └─ Write current user/assistant messages to ConversationMemoryStore
      └─ assistant metadata includes `compact_tool_calls` and compact response metadata
```

---

## 6. Route Judge

**File:** `agent/route_judge.py`

### 6.1 Core Interface

```python
class RouteJudge:
    async def plan(query, session_context, user_profile, available_tools) -> RouteDecision
```

### 6.2 RouteDecision Structure

| Field | Type | Description |
|---|---|---|
| `need_tools` | `bool` | Whether tools need to be invoked |
| `need_memory` | `bool` | Whether memory lookup is needed |
| `need_web_search` | `bool` | Whether web search is needed |
| `need_path_explanation` | `bool` | Whether path explanation is needed |
| `strategy` | `str` | Strategy name (for logging and debugging) |
| `tool_sequence` | `list[ToolCallPlan]` | Ordered tool invocation plan |
| `reason` | `str` | Routing decision rationale |
| `max_iterations` | `int` | Maximum execution steps |

### 6.3 Routing Logic (LLM + Rule Fallback)

The rule engine covers explicit patterns plus the default factual fallback:

| Scenario | Detection | Strategy | Tool Sequence |
|---|---|---|---|
| Simple greeting | `SIMPLE_PATTERN` | `simple_answer_no_tool` | Empty |
| KG ingestion | `INGEST_PATTERN` | `kg_ingest_request` | [web_search →] kg_ingest |
| Entity query | `ENTITY_PATTERN` | `graph_entity_lookup_first` | graph_entity_lookup → kg_hybrid_search |
| Relation/causal | `RELATION_PATTERN` | `kg_hybrid_first_then_graph_trace` | kg_hybrid_search → graph_relation_trace |
| Context follow-up | `FOLLOWUP_PATTERN` | `memory_first_then_*` | memory_search → subsequent tools |
| Real-time info | `REALTIME_PATTERN` | `freshness_aware_search` | web_search → kg_hybrid_search |
| User correction | `CORRECTION_PATTERN` + recent KG tools | `correction_and_refresh` | web_search |
| Direct URL query | `URL_PATTERN` | `direct_url_crawl` | web_search |
| Quant analysis | `QUANT_PATTERN` | `quant_request` | quant_backtest |
| Default factual QA | Fallback | `factual_qa` | kg_hybrid_search |

LLM refinement: When `llm_client` is available and the scenario is not simple/quant, the rule result is passed to the LLM for secondary adjustment (still constrained by `available_tools`; it will not invent nonexistent tools).

Route Judge prompt management now supports versioned templates through `agent/prompts.py`:

- `build_route_judge_prompt(..., prompt_version=...)` resolves the configured version before rendering
- `list_route_judge_prompt_versions()` exposes the registered versions
- unknown versions fall back to `v1` with a warning
- current built-in versions are:
  - `v1`: original concise route-refinement prompt
  - `v2`: more conservative refinement prompt that emphasizes minimal edits to the rule-based plan

`AgentCore` wires Route Judge to a utility-model client first. If `KG_AGENT_UTILITY_MODEL_*` / `UTILITY_LLM_*` is not configured, the module falls back to the main `KG_AGENT_MODEL_*` client and emits a one-time warning.

**Important dynamic-graph rules:**

- `correction_and_refresh` only fires when the current query matches correction intent and `session_context["recent_tool_calls"]` proves the previous assistant turn used KG retrieval tools
- `kg_ingest` is intentionally not inserted into realtime/correction `tool_sequence`; the bridge happens in `AgentCore`
- realtime freshness decisions currently rely on rule-based freshness checks, not on an LLM-only freshness classifier

---

## 7. Path Explainer

**File:** `agent/path_explainer.py`

### 7.1 Core Interface

```python
class PathExplainer:
    async def explain(query, graph_paths, evidence_chunks, domain_schema=None) -> PathExplanation
```

### 7.2 PathExplanation Structure

| Field | Type | Description |
|---|---|---|
| `enabled` | `bool` | Whether a valid explanation was generated |
| `question_type` | `str` | `relation_explanation` or `path_trace` |
| `core_entities` | `list[str]` | Core entity IDs (up to 3) |
| `paths` | `list[ExplainedPath]` | List of explained paths |
| `final_explanation` | `str` | Final explanation text |
| `uncertainty` | `str \| None` | Uncertainty note |

### 7.3 Two-Phase Strategy

1. **Candidate path scoring:** token overlap (query ∩ path) + evidence coverage (path ∩ chunks) + node count bonus
2. **Evidence matching:** select text from evidence_chunks most relevant to the best path
3. **LLM explanation generation:** invoke LLM when available, otherwise fall back to template concatenation
4. **No-result fallback:** returns `enabled=False` when paths or evidence are insufficient

### 7.4 Design Notes

- Domain-agnostic module, not limited to economics (`domain_schema` is an optional parameter)
- Does not modify the graph; only responsible for explanation
- Does not blindly trust graph relationships; requires "graph connectivity + text support"
- `AgentCore` wires Path Explainer through the same utility-model client used by Route Judge, with one-time fallback to the main agent model when no dedicated utility model is configured

---

## 8. Tool System

### 8.1 Tool Protocol

**Defined in** `tools/base.py`:

```python
@dataclass
class ToolDefinition:
    name: str                    # Tool name (globally unique)
    description: str             # Function description
    input_schema: dict           # JSON Schema
    handler: ToolHandler         # Async handler function
    enabled: bool = True         # Toggle
    tags: list[str] = []         # Category tags

@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict = {}
```

### 8.2 Registration Flow

```
tools/*.py  define async handler functions
    ↓
agent/tool_schemas.py  define parameter JSON Schemas
    ↓
agent/builtin_tools.py  build_default_tool_registry()
    ├─ Import handler + schema
    ├─ Construct ToolDefinition
    └─ registry.register(tool_definition)
    ↓
agent/agent_core.py  AgentCore.__init__() holds ToolRegistry
    ↓
Runtime → ToolRegistry.execute(name, **kwargs)
```

### 8.3 Current Tool Inventory

| Tool Name | File | Tags | Status | Enabled by Default |
|---|---|---|---|---|
| `kg_hybrid_search` | `tools/retrieval_tools.py` | retrieval, knowledge-graph | Implemented | **Yes** |
| `kg_naive_search` | `tools/retrieval_tools.py` | retrieval, vector | Implemented | **Yes** |
| `graph_entity_lookup` | `tools/graph_tools.py` | graph | Implemented | **Yes** |
| `graph_relation_trace` | `tools/graph_tools.py` | graph, explanation | Implemented | **Yes** |
| `memory_search` | `agent/builtin_tools.py` | memory | Implemented | **Yes** (controlled by `KG_AGENT_ENABLE_MEMORY`) |
| `cross_session_search` | `agent/builtin_tools.py` | memory, cross-session | Implemented | **Yes** when memory is enabled and a user-scoped cross-session store is available |
| `web_search` | `tools/web_search.py` | web | Implemented: direct URL crawling + DuckDuckGo search-engine URL discovery via Crawl4AI; supports `search_query` override | **No** (`KG_AGENT_ENABLE_WEB_SEARCH` defaults to false) |
| `kg_ingest` | `tools/kg_ingest.py` | knowledge-graph, ingestion | Implemented: accepts text/markdown from any source and calls `rag.ainsert()`; `source` accepts `str \| list[str] \| None` | **Yes** (controlled by `KG_AGENT_ENABLE_KG_INGEST`) |
| `quant_backtest` | `tools/quant_tools.py` | quant | Placeholder | **Yes** (controlled by `KG_AGENT_ENABLE_QUANT`) |

> **Note:** "Tool registered" does not mean "enabled by default". `GET /agent/tools` only returns tools with `enabled=True`.
> At default startup, 8 tools are available when `user_id` is present (`web_search` is disabled by default; `cross_session_search` is user-scoped).

### 8.5 Crawler Layer Architecture

The crawler layer (`kg_agent/crawler/`) provides the infrastructure behind `web_search`:

```
web_search(query, urls, top_k)
  ├─ _collect_urls()                       # Extract URLs from query text + urls arg
  ├─ [no URLs] → crawler_adapter.discover_urls(query, top_k)
  │    ├─ build_search_url(engine, query)  # DuckDuckGo HTML URL
  │    ├─ crawl_url(search_url)            # Crawl search results page
  │    └─ extract_search_results_from_markdown(markdown, query, top_k)
  │         ├─ _decode_search_redirect()   # DuckDuckGo /l/?uddg= redirect decoding
  │         ├─ _normalize_result_url()     # Strip tracking params, normalize path
  │         ├─ _score_discovered_result()  # Token overlap + URL structure + domain signals
  │         ├─ _is_blocked_result_domain() # Filter social media, etc.
  │         ├─ _is_generic_listing_result()# Filter tag/category/archive pages
  │         └─ Per-domain dedup + limit    # Max 2 results per domain
  └─ crawler_adapter.crawl_urls(target_urls, max_pages)
       └─ Crawl4AI AsyncWebCrawler
            ├─ Browser auto-detection (Playwright → system Edge/Chrome fallback)
            └─ _normalize_result() → CrawledPage
```

Recurring ingest uses the same crawler stack, but the scheduler layer now distinguishes generic pages from feed sources:

- `MonitoredSource.resolved_source_type()` auto-detects feed-like URLs when `source_type="auto"`
- `MonitoredSource.resolved_schedule_mode()` resolves `auto` to `adaptive_feed` for feed sources and `fixed` for normal page sources
- `IngestScheduler` tracks `consecutive_no_change` in crawl state and increases the effective poll interval for feed sources after repeated no-change polls; a successful new ingest resets that feed backoff
- For feed sources, the scheduler now calls `discover_feed_urls()` first, applies `feed_filter.include_patterns` / `exclude_patterns`, optional author/category/domain constraints, and optional `feed_filter.max_age_days` against feed entry metadata, then applies `feed_priority` ranking across the discovered feed entries before crawling only the selected article URLs
- `DiscoveredUrl.published_at` is populated from RSS `pubDate` / Atom `updated` or `published` when available; `DiscoveredUrl.author` and `DiscoveredUrl.categories` are also extracted from RSS/Atom feed entry metadata when present
- `CrawlStateRecord.recent_item_keys` maintains feed item recency ordering, `item_published_at` stores per-item timestamps when known, and `feed_retention.mode="latest"` / `feed_retention.max_age_days` prune `last_content_hashes` down to the retained tracked feed entries

**Key content_extractor constants:**

| Constant | Purpose |
|---|---|
| `BLOCKED_RESULT_DOMAINS` | Domains always excluded from discovery (social media, etc.) |
| `LOW_QUALITY_RESULT_DOMAINS` | Domains deprioritized in ranking |
| `GENERIC_PATH_SEGMENTS` | URL path segments indicating listing pages (tag, category, archive, etc.) |
| `TRACKING_QUERY_KEYS` | URL query parameters stripped during normalization (utm_*, ref, src, etc.) |

### 8.4 Web Search Semantics

`web_search` supports two execution paths depending on input:

**Path A — Direct URL crawling:**
When the query text contains URLs or `urls=[...]` is provided in tool args, crawls those URLs directly.

**Path B — Search-engine URL discovery:**
When no explicit URLs are available, invokes `crawler_adapter.discover_urls(query, top_k)` which:
1. Builds a DuckDuckGo HTML search URL via `content_extractor.build_search_url()`
2. Crawls the search results page
3. Extracts candidate links via `extract_search_results_from_markdown()` — decodes DuckDuckGo redirects, strips tracking params, scores results by query-token overlap / URL structure / domain quality
4. Filters out blocked domains (social media, etc.) and generic listing pages
5. Applies per-domain limits and returns top-k ranked `DiscoveredUrl` items
6. Crawls the discovered URLs and returns page data

When the active `LightRAG` instance has a configured rerank model (`rerank_model_func`), `web_search` expands the candidate crawl set, reranks successful page payloads against the effective query, and trims the final returned page list back to `top_k`. Returned page dicts include `rerank_score` when rerank is applied.

**Return data per page:**

- `title`, `final_url`, `excerpt`, `markdown`, `links`, `metadata`

**Error cases:**

| Condition | `status` | `success` |
|---|---|---|
| Crawler adapter not configured | `not_configured` | `false` |
| Discovery returned no URLs | `discovery_failed` | `false` |
| All crawled pages failed | `failed` | `false` |
| Some pages failed | `partial_success` | `true` |
| All pages succeeded | `success` | `true` |

### 8.6 Standard Steps for Adding a New Tool

1. Create a new file under `tools/`, implementing `async def my_tool(*, rag, query, ..., **_) -> ToolResult`
2. Add parameter JSON Schema in `tool_schemas.py`
3. Register in `build_default_tool_registry()` in `builtin_tools.py`
4. If toggle control is needed, add a corresponding field in `ToolConfig` in `config.py`
5. If routing support is needed, add a corresponding rule pattern in `route_judge.py`

### 8.7 Retrieval Freshness Decay

- `kg_hybrid_search` and `kg_naive_search` both accept internal `search_query`
- When `KG_AGENT_ENABLE_FRESHNESS_DECAY=true`, `retrieval_tools.py` passes freshness parameters into `QueryParam`
- Lower-layer freshness-aware ranking now runs inside `lightrag_fork/operate.py`
- `retrieval_tools.py` only keeps a compatibility fallback when lower-layer metadata does not mark `freshness_decay_applied`

---

## 9. Memory System

### 9.1 ConversationMemoryStore (In-Session Memory)

- Mongo backend reuses `MONGO_URI` / `MONGODB_URI` and `MONGO_DATABASE`; collection name is configurable

- Supports `memory`, `sqlite`, and `mongo` backends
- `append_message(session_id, role, content, metadata=None, user_id=None)` — append message
- `get_recent_history(session_id, turns)` — get last N conversation turns
- `get_context_window(session_id, query, turns, min_recent_turns, max_tokens)` — build a dynamic attention window: keep the newest turns, then backfill older query-relevant turns under a soft token budget
- `get_recent_tool_calls(session_id, assistant_turns)` — returns flattened compact tool history from recent assistant turns
- `search(session_id, query, limit)` — token-overlap search for relevant messages
- `search_user_sessions(user_id, query, limit, exclude_session_id=None)` — search same-user messages across sessions
- `clear_session(session_id)` — clear session
- The `memory_search` tool can request a larger candidate window and rerank the final current-session matches when the active `LightRAG` instance has `rerank_model_func` configured
- `AgentCore` now loads `session_context["history"]` through `get_context_window(...)` rather than blindly taking the last fixed N turns, so follow-up routing can retain older but still relevant facts

`AgentCore` currently stores the following assistant metadata into memory:

- `route_strategy`
- `compact_tool_calls`
- compact `response_metadata` subset, including `freshness_action` / `freshness_reason` when present

### 9.2 CrossSessionStore (Cross-Session)

- Supports `memory`, `auto`, and `mongo_qdrant` modes
- When `mongo_qdrant` is enabled, message documents are written to Mongo and embeddings are written to Qdrant
- Reuses `.env` Mongo / Qdrant settings (`MONGO_URI` or `MONGODB_URI`, `MONGO_DATABASE`, `QDRANT_URL`, `QDRANT_API_KEY`)
- `index_message(message)` stores user-scoped messages for future cross-session retrieval
- Write path includes low-signal filtering, whitespace normalization, duplicate-sentence removal, bounded content compression, and stable fingerprint deduplication
- Repeated same-user messages with the same normalized content are merged into one vector memory record using `occurrence_count`, `first_seen_at`, `last_seen_at`, and bounded `session_ids`
- Optional semantic consolidation merges highly similar same-user messages into one clustered memory record and regenerates a bounded summary from retained snippets
- Consolidated records keep `member_fingerprints`, bounded `source_snippets`, `cluster_size`, and `summary_strategy`
- Optional background maintenance can periodically re-consolidate existing memories, compact stale records, and delete low-support aged singleton memories
- Aging uses bounded snippet retention and summary rebuild rather than preserving every historical snippet forever
- `search(user_id, query, limit, exclude_session_id=None)` first tries vector search, then falls back to `ConversationMemoryStore.search_user_sessions(...)`
- Cross-session lookup is exposed as a first-class tool: `cross_session_search`
- The `cross_session_search` tool also supports optional reranking of retrieved memory candidates through the active `LightRAG` rerank configuration

### 9.3 UserProfileStore (User Profile)

- Mongo backend reuses `MONGO_URI` / `MONGODB_URI` and `MONGO_DATABASE`; collection name is configurable

- Supports `memory`, `sqlite`, and `mongo` backends
- `get_profile(user_id)` — get profile attributes
- `update_profile(user_id, attributes)` — update profile

---

## 10. Configuration System

**File:** `config.py`

All configuration is read from environment variables with sensible defaults.

### 10.1 Model Configuration (AgentModelConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_MODEL_PROVIDER` | `openai_compatible` | Model provider |
| `KG_AGENT_MODEL_NAME` | Falls back to `LLM_MODEL` → `OPENAI_MODEL` | Model name |
| `KG_AGENT_MODEL_BASE_URL` | Falls back to `OPENAI_BASE_URL` → `LLM_BINDING_HOST` | API URL |
| `KG_AGENT_MODEL_API_KEY` | Falls back to `OPENAI_API_KEY` → `LLM_BINDING_API_KEY` | API key |
| `KG_AGENT_MODEL_TIMEOUT_S` | `60.0` | Timeout |

Optional lightweight utility model for internal routing/explanation work:

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_UTILITY_MODEL_PROVIDER` | `openai_compatible` | Utility model provider |
| `KG_AGENT_UTILITY_MODEL_NAME` | empty | Dedicated utility model name |
| `KG_AGENT_UTILITY_MODEL_BASE_URL` | empty | Dedicated utility model API URL |
| `KG_AGENT_UTILITY_MODEL_API_KEY` | empty | Dedicated utility model API key |
| `KG_AGENT_UTILITY_MODEL_TIMEOUT_S` | Falls back to `UTILITY_LLM_TIMEOUT` → `60.0` | Dedicated utility model timeout |
| `UTILITY_LLM_PROVIDER` | `openai_compatible` | Cross-layer fallback provider key also understood by `kg_agent` |
| `UTILITY_LLM_MODEL` | empty | Cross-layer fallback utility model name |
| `UTILITY_LLM_BINDING_HOST` | empty | Cross-layer fallback utility model API URL |
| `UTILITY_LLM_BINDING_API_KEY` | empty | Cross-layer fallback utility model API key |
| `UTILITY_LLM_TIMEOUT` | `60.0` | Cross-layer fallback utility model timeout |

### 10.2 Tool Switches (ToolConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_ENABLE_WEB_SEARCH` | `false` | Enable web search |
| `KG_AGENT_ENABLE_MEMORY` | `true` | Enable conversation memory |
| `KG_AGENT_ENABLE_QUANT` | `true` | Enable quantitative tools |
| `KG_AGENT_ENABLE_KG_INGEST` | `true` | Enable KG ingestion tool |

### 10.3 Runtime Configuration (AgentRuntimeConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_DEFAULT_WORKSPACE` | Falls back to `WORKSPACE` | Default workspace |
| `KG_AGENT_DEFAULT_DOMAIN_SCHEMA` | `general` | Default domain schema |
| `KG_AGENT_MAX_ITERATIONS` | `3` | Maximum tool invocation rounds |
| `KG_AGENT_ROUTE_JUDGE_PROMPT_VERSION` | `v1` | Version key for Route Judge LLM refinement prompt templates |
| `KG_AGENT_MEMORY_WINDOW_TURNS` | `6` | Maximum number of conversation turns considered for the dynamic attention window |
| `KG_AGENT_MEMORY_MIN_RECENT_TURNS` | `2` | Minimum number of newest turns always kept in the dynamic attention window |
| `KG_AGENT_MEMORY_MAX_CONTEXT_TOKENS` | `1200` | Soft token budget used when selecting conversation history for routing/final answer prompts |
| `KG_AGENT_DEBUG` | `false` | Debug mode |

### 10.4 Crawler Configuration (CrawlerConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_WEB_CRAWLER_PROVIDER` | `crawl4ai` | Current crawler backend |
| `KG_AGENT_WEB_CRAWLER_BROWSER_TYPE` | `chromium` | Browser family for Crawl4AI |
| `KG_AGENT_WEB_CRAWLER_BROWSER_CHANNEL` | empty | Preferred system browser channel (`chrome` / `msedge`) |
| `KG_AGENT_WEB_CRAWLER_HEADLESS` | `true` | Run browser headlessly |
| `KG_AGENT_WEB_CRAWLER_VERBOSE` | `false` recommended | Browser/crawler verbosity |
| `KG_AGENT_WEB_CRAWLER_CACHE_MODE` | `BYPASS` | Crawl4AI cache mode |
| `KG_AGENT_WEB_CRAWLER_MAX_PAGES` | `3` | Maximum pages per crawl request |
| `KG_AGENT_WEB_CRAWLER_MAX_CONTENT_CHARS` | `4000` | Maximum extracted content length per page |
| `KG_AGENT_WEB_CRAWLER_WORD_COUNT_THRESHOLD` | `20` | Minimum content threshold |
| `KG_AGENT_WEB_CRAWLER_PAGE_TIMEOUT_MS` | `30000` | Per-page timeout |

### 10.5 Scheduler Configuration (SchedulerConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_ENABLE_SCHEDULER` | `false` | Enable recurring crawl scheduler |
| `KG_AGENT_SCHEDULER_CHECK_INTERVAL` | `60` | Scheduler loop interval in seconds |
| `KG_AGENT_SCHEDULER_SOURCES_FILE` | empty | Optional JSON file for source persistence |
| `KG_AGENT_SCHEDULER_STATE_FILE` | `scheduler_state.json` | JSON file for crawl state persistence |
| `KG_AGENT_SCHEDULER_ENABLE_LEADER_ELECTION` | `false` | Enable scheduler loop leader election |
| `KG_AGENT_SCHEDULER_LOOP_LEASE_KEY` | `scheduler:loop` | Shared coordination key used for recurring loop ownership |
| `KG_AGENT_SCHEDULER_COORDINATION_BACKEND` | `auto` | `auto`, `local`, or `redis` for scheduler coordination |
| `KG_AGENT_SCHEDULER_COORDINATION_REDIS_URL` | empty | Redis URL for scheduler coordination when Redis backend is used |
| `KG_AGENT_SCHEDULER_COORDINATION_TTL_SECONDS` | `120` | Lease TTL for scheduler coordination and loop ownership |

Per-source scheduler behavior is controlled through `MonitoredSource` rather than global env vars:

- `source_type`: `auto`, `page`, or `feed`
- `schedule_mode`: `auto`, `fixed`, or `adaptive_feed`
- Feed sources default to `adaptive_feed` when `schedule_mode="auto"`; repeated `no_change` polls increase `effective_interval_seconds`, while newly ingested content resets the no-change streak
- `feed_filter.include_patterns`: optional case-insensitive substring allowlist over feed entry title + URL
- `feed_filter.exclude_patterns`: optional case-insensitive substring denylist over feed entry title + URL
- `feed_filter.include_authors` / `exclude_authors`: optional case-insensitive substring filters over parsed feed author metadata
- `feed_filter.include_categories` / `exclude_categories`: optional case-insensitive substring filters over parsed feed category/tag metadata
- `feed_filter.allowed_domains` / `blocked_domains`: optional host-based filters; subdomains match parent domains
- `feed_filter.max_age_days`: optional age filter; when `published_at` is available, entries older than this threshold are skipped before crawl
- `feed_retention.mode`: `keep_all` or `latest`
- `feed_retention.max_items`: required when `feed_retention.mode="latest"`; caps the number of tracked feed items retained in scheduler state
- `feed_retention.max_age_days`: optional state-pruning threshold; when tracked feed items have `published_at`, items older than this threshold are removed from scheduler state even if their content hashes still exist
- `feed_priority.mode`: `auto`, `feed_order`, `published_desc`, or `priority_score`; `auto` resolves to `priority_score` when any preference signals are configured, otherwise `feed_order`
- `feed_priority.priority_patterns`: optional case-insensitive substring boosts over feed entry title + URL
- `feed_priority.preferred_domains`: optional host-based boost list; subdomains match parent domains
- `feed_priority.preferred_authors`: optional case-insensitive substring boosts over parsed feed author metadata
- `feed_priority.preferred_categories`: optional case-insensitive substring boosts over parsed feed category/tag metadata

### 10.6 Persistence Configuration (PersistenceConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_MEMORY_BACKEND` | `memory` | `memory`, `sqlite`, or `mongo` for conversation memory |
| `KG_AGENT_MEMORY_SQLITE_PATH` | `kg_agent_memory.sqlite3` | SQLite file used by conversation memory |
| `KG_AGENT_MEMORY_MONGO_COLLECTION` | `kg_agent_conversation_messages` | Mongo collection used for conversation memory messages |
| `KG_AGENT_USER_PROFILE_BACKEND` | `memory` | `memory`, `sqlite`, or `mongo` for user profiles |
| `KG_AGENT_USER_PROFILE_SQLITE_PATH` | `kg_agent_profiles.sqlite3` | SQLite file used by user profiles |
| `KG_AGENT_USER_PROFILE_MONGO_COLLECTION` | `kg_agent_user_profiles` | Mongo collection used for user profiles |
| `KG_AGENT_SCHEDULER_STORE_BACKEND` | `json` | `json` or `sqlite` for source/state persistence |
| `KG_AGENT_SCHEDULER_STORE_SQLITE_PATH` | `kg_agent_scheduler.sqlite3` | SQLite file used by scheduler source/state stores |
| `KG_AGENT_CROSS_SESSION_BACKEND` | `memory` | `memory`, `auto`, or `mongo_qdrant` for cross-session retrieval |
| `KG_AGENT_CROSS_SESSION_MONGO_COLLECTION` | `kg_agent_cross_session_messages` | Mongo collection used for cross-session message documents |
| `KG_AGENT_CROSS_SESSION_QDRANT_COLLECTION_PREFIX` | `kg_agent_cross_session` | Qdrant collection prefix; actual collection adds embedding model suffix |
| `KG_AGENT_CROSS_SESSION_MIN_CONTENT_CHARS` | `8` | Minimum normalized message length before vector indexing |
| `KG_AGENT_CROSS_SESSION_MAX_CONTENT_CHARS` | `1200` | Maximum indexed content length after compression |
| `KG_AGENT_CROSS_SESSION_MAX_SESSION_REFS` | `12` | Maximum number of session IDs retained on a deduplicated memory record |
| `KG_AGENT_CROSS_SESSION_ENABLE_CONSOLIDATION` | `true` | Enable semantic consolidation of similar cross-session memories |
| `KG_AGENT_CROSS_SESSION_CONSOLIDATION_SIMILARITY_THRESHOLD` | `0.82` | Minimum vector similarity required before merging memories into one cluster |
| `KG_AGENT_CROSS_SESSION_CONSOLIDATION_TOP_K` | `3` | Number of nearest same-user memory candidates checked for consolidation |
| `KG_AGENT_CROSS_SESSION_MAX_CLUSTER_SNIPPETS` | `6` | Maximum number of retained snippets used to rebuild a consolidated memory summary |
| `KG_AGENT_CROSS_SESSION_ENABLE_BACKGROUND_MAINTENANCE` | `false` | Enable periodic background re-consolidation and memory aging |
| `KG_AGENT_CROSS_SESSION_MAINTENANCE_INTERVAL_SECONDS` | `1800` | Interval between background maintenance passes |
| `KG_AGENT_CROSS_SESSION_MAINTENANCE_BATCH_SIZE` | `100` | Maximum number of stored memories processed per maintenance pass |
| `KG_AGENT_CROSS_SESSION_AGING_STALE_AFTER_DAYS` | `14.0` | Age threshold after which memories are compacted into stale summaries |
| `KG_AGENT_CROSS_SESSION_AGING_DELETE_AFTER_DAYS` | `60.0` | Age threshold after which low-support memories may be deleted |
| `KG_AGENT_CROSS_SESSION_AGING_KEEP_MIN_OCCURRENCES` | `2` | Minimum support required to protect an old memory from aging-based deletion |
| `KG_AGENT_CROSS_SESSION_AGING_MAX_SNIPPETS` | `3` | Maximum snippets retained when rebuilding an aged memory summary |

### 10.7 Freshness Configuration (FreshnessConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_FRESHNESS_THRESHOLD_SECONDS` | `604800` | Freshness threshold for graph age checks |
| `KG_AGENT_ENABLE_AUTO_INGEST` | `false` | Allow realtime auto-ingest bridge during chat |
| `KG_AGENT_STALENESS_DECAY_DAYS` | `7.0` | Half-life parameter for freshness-aware KG retrieval decay |
| `KG_AGENT_ENABLE_FRESHNESS_DECAY` | `false` | Enable freshness-aware KG retrieval decay |

### 10.8 Rerank Configuration

`build_rag_from_env()` now forwards the standard LightRAG rerank environment variables into the agent-managed `LightRAG` instance, and tool-level query flows (`memory_search`, `cross_session_search`, `web_search`) reuse that same rerank configuration through `rag.rerank_model_func` / `rag.min_rerank_score`.

| Environment Variable | Default | Description |
|---|---|---|
| `RERANK_BINDING` | `null` | Rerank provider binding (`cohere`, `jina`, `aliyun`, or disabled) |
| `RERANK_MODEL` | Provider default | Rerank model name |
| `RERANK_BINDING_HOST` | Provider default | Rerank API base URL |
| `RERANK_BINDING_API_KEY` | empty | Rerank API key |
| `MIN_RERANK_SCORE` | `0.0` | Minimum rerank score threshold applied after reranking |
| `RERANK_ENABLE_CHUNKING` | `false` | Cohere-specific chunked rerank mode for long documents |
| `RERANK_MAX_TOKENS_PER_DOC` | `4096` | Cohere-specific max token count per rerank document |

---

## 11. API Endpoints

**Files:** `api/agent_routes.py`, `api/app.py`

| Method | Path | Defined In | Description |
|---|---|---|---|
| POST | `/agent/chat` | `agent_routes.py` | Main conversation entry point |
| POST | `/agent/ingest` | `agent_routes.py` | Insert content into KG (web page, PDF, manual text, etc.) |
| GET | `/agent/tools` | `agent_routes.py` | View currently enabled tools |
| POST | `/agent/route_preview` | `agent_routes.py` | Return routing plan only (for debugging) |
| GET | `/agent/scheduler/status` | `agent_routes.py` | View scheduler runtime/source status |
| GET | `/agent/sources` | `agent_routes.py` | List monitored crawl sources |
| POST | `/agent/sources` | `agent_routes.py` | Add or update a monitored source |
| DELETE | `/agent/sources/{source_id}` | `agent_routes.py` | Remove a monitored source |
| POST | `/agent/sources/{source_id}/trigger` | `agent_routes.py` | Trigger immediate crawl/ingest for one source |
| GET | `/health` | `app.py` | Health check + workspace status |

### 11.1 POST /agent/chat

**Request Body (ChatRequest):**

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | `str` | Yes | User question |
| `session_id` | `str` | Yes | Session ID |
| `user_id` | `str` | No | User ID |
| `workspace` | `str` | No | Workspace |
| `domain_schema` | `str \| dict` | No | Domain schema (e.g. `"economy"`) |
| `max_iterations` | `int` | No | Maximum tool invocation rounds (1–8) |
| `use_memory` | `bool` | No | Enable memory (default true) |
| `debug` | `bool` | No | Return full tool data |
| `stream` | `bool` | No | When `true`, return SSE events for route selection, tool execution, optional path explanation, and final answer generation |

**Response Body (ChatResponse):**

| Field | Type | Description |
|---|---|---|
| `answer` | `str` | Final answer |
| `route` | `dict` | Routing decision details |
| `tool_calls` | `list[dict]` | Tool invocation records |
| `path_explanation` | `dict \| null` | Path explanation result |
| `metadata` | `dict` | Metadata, including dynamic-graph markers such as `freshness_action` / `freshness_reason` when applicable |

**Streaming SSE events (`stream=true`):**

- `meta` — initial stream metadata (`streaming_supported`, session/workspace metadata)
- `route` — structured route decision before tool execution
- `tool_start` — one tool execution is beginning
- `tool_result` — one tool execution finished; includes serialized tool result
- `tool_skip` — optional remaining tools were skipped after an earlier successful result
- `status` — metadata update emitted after freshness/correction dynamic-update handling
- `path_explanation_start` — path explanation stage has started
- `path_explanation` — path explanation payload before final answer generation
- `answer_start` — final answer generation stage has started
- `delta` — incremental answer text chunk
- `done` — terminal payload with final answer, route, tool calls, path explanation, and metadata

### 11.2 POST /agent/ingest

**Request Body (IngestRequest):**

| Field | Type | Required | Description |
|---|---|---|---|
| `content` | `str \| list[str]` | Yes | Text or markdown content to ingest |
| `source` | `str \| list[str]` | No | Provenance label (URL, file path, or descriptive tag) |
| `workspace` | `str` | No | Target workspace (uses default if omitted) |

**Response Body (IngestResponse):**

| Field | Type | Description |
|---|---|---|
| `status` | `str` | `"accepted"` on success |
| `track_id` | `str \| null` | LightRAG pipeline tracking ID |
| `document_count` | `int` | Number of documents accepted |
| `source` | `str \| null` | Echoed provenance label |
| `message` | `str` | Human-readable status message |

### 11.3 Scheduler and Source APIs

- `GET /agent/scheduler/status`
  - Returns scheduler configured/enabled/running flags, persistence file paths, loop counters, and per-source status including resolved source type / schedule mode / feed priority mode, `consecutive_no_change`, `tracked_item_count`, and `effective_interval_seconds`
- `GET /agent/sources`
  - Returns all monitored sources, including `source_type`, `schedule_mode`, `feed_filter`, `feed_retention`, `feed_priority`, `resolved_source_type`, `resolved_schedule_mode`, and `resolved_feed_priority_mode`
- `POST /agent/sources`
  - Upserts a `MonitoredSource`; request payload accepts `source_type`, `schedule_mode`, `feed_filter`, `feed_retention`, and `feed_priority`
- `DELETE /agent/sources/{source_id}`
  - Removes a source and its state record
- `POST /agent/sources/{source_id}/trigger`
  - Immediately polls a source and returns crawl/ingest counts

### 11.4 GET /health

**Response Body:**

| Field | Type | Description |
|---|---|---|
| `status` | `str` | Fixed `"ok"` |
| `service` | `str` | Fixed `"kg_agent"` |
| `workspace` | `str` | Current default workspace |
| `default_workspace` | `str` | Default workspace resolved from config/environment |
| `rag_bootstrapped` | `bool` | Whether the app has a usable fixed rag instance or a ready rag_provider bootstrap path |
| `dynamic_workspace_enabled` | `bool` | Whether dynamic workspace (provider mode) is in use |
| `active_workspaces` | `list[str]` | List of currently loaded workspaces |
| `active_workspace_count` | `int` | Number of currently loaded workspaces |
| `scheduler_configured` | `bool` | Whether scheduler was attached to the app |
| `scheduler_enabled` | `bool` | Whether scheduler is enabled by config |
| `scheduler_running` | `bool` | Whether scheduler background task is currently running |

---

## 12. Development Commands

```bash
# Activate virtual environment
.venv\Scripts\Activate.ps1                     # Windows PowerShell
source .venv/bin/activate                       # Linux/Mac

# Start kg_agent API service (requires LightRAG backend services: Neo4j / Qdrant / MongoDB / LLM)
# Recommended (registered as pyproject.toml entry point):
kg-agent-server

# Or using uv:
uv run kg-agent-server

# Or direct module start:
python -m kg_agent.api.app

# Default port 9721, configurable via KG_AGENT_PORT environment variable
# Default host 0.0.0.0, configurable via KG_AGENT_HOST environment variable
```

**Environment variable requirements:** `.env` file in the project root, containing at minimum:

- LLM config: `LLM_MODEL`, `LLM_BINDING`, `LLM_BINDING_HOST`, `LLM_BINDING_API_KEY`
- Storage config (required by LightRAG backend): `NEO4J_URI`, `QDRANT_URL`, `MONGO_URI`, etc.
- Optional Agent-specific config: `KG_AGENT_MODEL_NAME`, `KG_AGENT_ENABLE_*`

**Crawl4AI requirements (when enabling `web_search`):**

1. Install API dependencies so `crawl4ai` is present in `.venv`
2. Enable the tool:
   - `KG_AGENT_ENABLE_WEB_SEARCH=true`
3. Recommended on Windows:
   - set `KG_AGENT_WEB_CRAWLER_BROWSER_CHANNEL=chrome` or `msedge`
   - this lets Crawl4AI reuse an already installed system browser and avoids slow Playwright browser downloads
4. If you intentionally use Playwright-managed browsers instead of system Chrome/Edge, `crawl4ai-setup` may download/update browser runtimes under:
   - `%LOCALAPPDATA%\\ms-playwright\\`
   - this is separate from your normal Chrome/Edge installation

**Important Windows note:**  
The adapter suppresses Crawl4AI run-time console logging because Rich console output can hit `UnicodeEncodeError` under legacy GBK terminals.

---

## 13. Guidelines for Modifying This Directory

### 13.1 Safety Checklist

- [ ] Does it introduce write modifications to `lightrag_fork/`? (Forbidden)
- [ ] Is the new tool registered via `ToolRegistry.register()`?
- [ ] Does the new tool handler follow the `async def tool(*, rag, query, ..., **_) -> ToolResult` signature?
- [ ] Do new Route Judge rules only reference tool names from `available_tools`?
- [ ] Do new environment variables have sensible defaults?
- [ ] Does it depend on LangChain or other heavy frameworks? (Forbidden)
- [ ] Does path explanation retain a fallback path?

### 13.2 Common Modification Patterns

| Scenario | Recommended Approach |
|---|---|
| Add a new tool | Create file in `tools/` → add schema in `tool_schemas.py` → register in `builtin_tools.py` |
| Modify routing strategy | Add regex pattern in `route_judge.py` or adjust LLM prompt |
| Improve path explanation | Adjust scoring/evidence selection logic in `path_explainer.py` |
| Extend search-engine discovery | Add engine support in `content_extractor.build_search_url()` and adapter |
| Integrate quant service | Replace placeholder implementation in `tools/quant_tools.py` |
| Persist conversation memory | Use `sqlite` or `mongo` backend in `memory/conversation_memory.py`, or add a new backend following the same pattern |
| Cross-session memory | Extend `memory/cross_session_store.py` backends or ranking logic |
| Persist user profiles | Use `sqlite` or `mongo` backend in `memory/user_profile.py`, or add a new backend following the same pattern |
| Add API routes | Create new route file under `api/`, `include_router` in `app.py` |

### 13.3 Things Not to Do in This Directory

- Directly modify files in `lightrag_fork/`
- Hardcode tool invocation logic in `agent_core.py` (use ToolRegistry instead)
- Expose RouteJudge or PathExplainer as standalone chat endpoints
- Introduce LangChain / LlamaIndex or other heavy frameworks as core Agent dependencies

---

## 14. Key Classes and Functions Quick Reference

| Class/Function | File | Purpose |
|---|---|---|
| `AgentCore` | `agent/agent_core.py` | Main Agent: `.chat()` conversation entry point |
| `AgentRunContext` | `agent/agent_core.py` | Single conversation run context |
| `AgentResponse` | `agent/agent_core.py` | Conversation return structure |
| `RouteJudge` | `agent/route_judge.py` | Route Judge: `.plan()` returns RouteDecision |
| `RouteDecision` | `agent/route_judge.py` | Structured routing plan |
| `ToolCallPlan` | `agent/route_judge.py` | Single-step tool invocation plan |
| `PathExplainer` | `agent/path_explainer.py` | Path Explainer: `.explain()` returns PathExplanation |
| `PathExplanation` | `agent/path_explainer.py` | Path explanation result |
| `ExplainedPath` | `agent/path_explainer.py` | Single explained path |
| `ToolRegistry` | `agent/tool_registry.py` | Tool registration and execution |
| `ToolDefinition` | `tools/base.py` | Tool definition data structure |
| `ToolResult` | `tools/base.py` | Tool execution result |
| `kg_ingest()` | `tools/kg_ingest.py` | Generic KG ingestion tool |
| `build_default_tool_registry()` | `agent/builtin_tools.py` | Build default tool set |
| `KGAgentConfig` | `config.py` | Unified configuration entry point |
| `AgentLLMClient` | `config.py` | OpenAI-compatible LLM client |
| `FallbackLLMClient` | `config.py` | Utility-model-first client wrapper with one-time fallback warning |
| `ConversationMemoryStore` | `memory/conversation_memory.py` | In-session memory with optional SQLite or Mongo persistence |
| `CrossSessionStore` | `memory/cross_session_store.py` | Cross-session retrieval over same-user prior sessions with optional Mongo/Qdrant vector backend |
| `UserProfileStore` | `memory/user_profile.py` | User profile with optional SQLite or Mongo persistence |
| `EnvLightRAGProvider` | `api/app.py` | Dynamic workspace LightRAG instance management |
| `build_rag_from_env()` | `api/app.py` | Build LightRAG instance from environment variables |
| `create_app()` | `api/app.py` | FastAPI app factory |
| `create_agent_routes()` | `api/agent_routes.py` | API route registration |

---

## 15. Known Limitations and TODOs

- **quant_backtest** placeholder not implemented; needs integration with quantitative engine
- **Scheduler** now supports optional loop leader election plus source-level coordination, but it still does not implement scheduler sharding or a richer distributed control plane
- **Source persistence** now supports `json` and `sqlite`, but there is still no Redis/Mongo/Postgres-backed scheduler registry/state implementation
- **RSS / Feed source support** now includes auto-detected feed typing, `adaptive_feed` scheduling, entry filtering, RSS/Atom `published_at` extraction, author/category/domain-aware filtering, and both latest-item and age-based retention, but discovery/management is still URL-centric and does not yet provide richer feed semantics such as feed-native ranking, source credibility scoring, or full feed graph provenance
- **Freshness decay** now has lower-layer support through `QueryParam.enable_freshness_decay` and `lightrag_fork/operate.py`, while retrieval-layer fallback remains for compatibility
- **Playwright browser detection** uses Playwright sync API to verify the exact chromium executable exists; on version mismatch the adapter falls back to system Edge/Chrome via `browser_channel`
- **CrossSessionStore** now supports an optional Mongo + Qdrant vector backend with heuristic compression and dedup, but it still falls back to conversation-memory token overlap when the vector backend is disabled or unavailable
- Cross-session consolidation and background aging are heuristic only; they use vector similarity plus lightweight lexical guards and snippet packing, not LLM summarization or a full long-horizon memory graph
- Path explanation scoring algorithm is based on simple token overlap; semantic similarity can be integrated later
