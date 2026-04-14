# AGENTS.md — kg_agent

> This file is for AI coding assistants (Copilot / Cursor / Cline, etc.) to reference when modifying code in `kg_agent/`.
> Human developers can also use it as an architecture quick-reference.

---

## 1. Module Positioning

`kg_agent/` is the **business layer** of this project, building an agent system on top of `lightrag_fork/` (graph-vector backend).

**Core Responsibilities:**

- Agent main loop (reasoning → routing → tool invocation → summary response)
- Route Judge: decides "how to query, what to query, how many steps"
- Local skill catalog / loader / executor for `skills/*/SKILL.md`
- Path Explainer: organizes graph paths + text evidence into readable explanations
- Tool registration and execution for the built-in core capability set (retrieval, graph, memory, web search, ingestion)
- Static MCP-backed external capability execution through the capability layer
- Conversation memory (dynamic attention window + cross-session + user profile)
- External API service

**Not Responsible For:**

- Document chunking, entity extraction, graph merging, vector writes → handled by `lightrag_fork/`
- Storage backend management (Neo4j / Qdrant / MongoDB, etc.) → handled by `lightrag_fork/kg/`
- LLM low-level adaptation (model call abstraction) → provided by `lightrag_fork/llm/`, wrapped by `AgentLLMClient` in this layer
- Domain-specific external capabilities (for example quant/backtest engines) should not be embedded as placeholder built-in tools; they belong in external MCP/skill-style integrations through the capability layer

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
│   ├── capability_registry.py     # CapabilityDefinition/CapabilityRegistry: planner-visible capability layer
│   ├── route_judge.py             # Route Judge: RouteJudge.plan() → RouteDecision
│   ├── path_explainer.py          # Path explanation layer: PathExplainer.explain() → PathExplanation
│   ├── tool_registry.py           # Tool registration protocol: ToolRegistry (register, find, execute)
│   ├── builtin_tools.py           # Assembly layer: wraps functions from tools/ and registers them to ToolRegistry
│   ├── prompts.py                 # All prompt templates (route judge version registry / path-explainer template registry / final answer)
│   └── tool_schemas.py            # Tool parameter JSON Schema definitions
│
├── skills/                        # Local skill layer
│   ├── registry.py                # SkillRegistry: scan `skills/*/SKILL.md` into a lightweight catalog
│   ├── loader.py                  # SkillLoader: progressive skill content loading + file reads
│   ├── command_planner.py         # SkillCommandPlanner: LoadedSkill + request -> SkillCommandPlan
│   ├── executor.py                # SkillExecutor: loader -> command planner -> runtime handoff
│   ├── runtime_client.py          # Optional runtime transport clients (for example MCP-backed skill runtime)
│   └── models.py                  # SkillDefinition / SkillPlan / LoadedSkill / SkillCommandPlan / SkillRunRecord dataclasses
│
├── mcp/                           # External capability transport layer
│   ├── __init__.py
│   └── adapter.py                 # MCPAdapter: stdio-based external capability execution
│
├── tools/                         # General tool layer (one file per tool)
│   ├── __init__.py
│   ├── base.py                    # ToolDefinition + ToolResult data structures
│   ├── retrieval_tools.py         # kg_hybrid_search, kg_naive_search
│   ├── graph_tools.py             # graph_entity_lookup, graph_relation_trace
│   ├── kg_ingest.py               # kg_ingest: insert any text/markdown into KG via rag.ainsert()
│   └── web_search.py              # web_search: URL crawling + search-engine URL discovery via Crawl4AI
│
├── memory/                        # Memory system
│   ├── __init__.py
│   ├── conversation_memory.py     # ConversationMemoryStore: in-session message storage/search with memory/SQLite/Mongo backends
│   ├── cross_session_store.py     # CrossSessionStore: same-user retrieval across prior sessions
│   └── user_profile.py            # UserProfileStore: user profile storage with memory/SQLite/Mongo backends
│
├── crawler/                       # Web crawling layer
│   ├── __init__.py                # Exports Crawl4AIAdapter, CrawledPage, DiscoveredUrl, IngestScheduler, source/state types
│   ├── crawler_adapter.py         # Crawl4AIAdapter: start/close/crawl_url/crawl_urls/discover_urls; `crawl_urls()` batches article crawls through Crawl4AI `arun_many()` and feed parsing also extracts published_at from RSS/Atom entries
│   ├── content_extractor.py       # Search-result extraction, URL scoring/ranking, Markdown normalization, DuckDuckGo redirect decoding
│   ├── source_registry.py         # MonitoredSource (page/feed typing + feed filter/retention/priority/dedup + content lifecycle + event-cluster policy), SourceRegistry, Json/SQLite source persistence
│   ├── crawl_state_store.py       # CrawlStateRecord (hashes + recent feed item history + item published_at/content-fingerprint maps + active doc IDs/expiry metadata + per-item event-cluster maps/records + failure/no-change streaks), CrawlStateStore, Json/SQLite state persistence
│   └── scheduler.py               # IngestScheduler: recurring crawl + content-hash dedup + feed-aware adaptive scheduling/filtering/priority/dedup/retention + short-term lifecycle + workspace-aware event clustering
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

### 3.4 Capabilities Are Exposed Through CapabilityRegistry; Skills Are Separate

- All tools must be registered via `ToolRegistry.register()` before use
- Planner-visible built-in capabilities are registered via `CapabilityRegistry`
- `AgentCore` currently builds the native capability registry by mirroring the enabled/disabled built-in tool set
- Configured MCP capabilities are also registered in `CapabilityRegistry` with `kind="external_mcp"` and `executor="mcp"`
- `SkillRegistry` scans `./skills/*/SKILL.md` into an independent planner-facing skill catalog (`name`, `description`, optional `tags`, local path)
- `AgentCore` passes both the capability catalog and the skill catalog into `RouteJudge`
- Do not hardcode tool invocations in `agent_core.py`
- `ToolRegistry` is for the core built-in capability set only
- Do not model local skills as planner-visible MCP tools
- Do not add domain-specific placeholder tools to the core registry; specialized external modules should be exposed through the MCP adapter instead

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
- `MonitoredSource.feed_dedup` now controls feed content-level duplicate suppression: `mode="content_hash"` suppresses exact normalized-content repeats across different feed URLs, while `mode="content_signature"` suppresses near-duplicate updates by hashing the leading normalized content tokens
- `MonitoredSource.content_lifecycle` now classifies crawler output as `long_term_knowledge` or `short_term_news` and controls whether updates append or replace the currently active document for each tracked item
- Short-term feed sources can also enable event clustering through `content_lifecycle.event_cluster_mode`; `auto` resolves to heuristic clustering by default and upgrades to `heuristic_llm` when a Utility LLM client is available at runtime, and the scheduler can now also use workspace-global `chunks_vdb` candidates to reuse an existing news-event cluster across different feed sources in the same workspace
- Feed-aware source policies also support publication-time windows: `feed_filter.max_age_days` skips stale entries before crawl, while `feed_retention.max_age_days` prunes old tracked feed items from scheduler state when entry timestamps are available
- Feed discovery and scheduler state tracking now use canonical article URLs: fragments are dropped, default ports and repeated slashes are normalized, and tracking query params (`utm_*`, `ref`, `src`, etc.) are stripped before feed entry deduplication, no-change detection, and retained item bookkeeping
- `CrawlStateRecord` now also tracks item-level active crawler doc IDs, doc expiry timestamps, `item_event_cluster_ids`, and `event_clusters` so short-term sources can supersede related feed articles and optionally delete stale documents without changing `lightrag_fork`
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
   ├─ Available capability list + planner-facing capability catalog (enabled capabilities from CapabilityRegistry; native capabilities are planner-visible by default, configured MCP capabilities are hidden from auto-routing unless `planner_exposed=true`)
   └─ Available skills from `SkillRegistry` (lightweight local catalog only; full skill contents are loaded later on demand)

2. Invoke Route Judge
   └─ RouteJudge.plan(query, session_context, user_profile, available_capabilities, available_capability_catalog, available_skills)
       ├─ Rule engine priority (regex matching + capability matching + skill matching)
       └─ Optional LLM refinement (when llm_client is available, LLM can adjust rule results using both planner surfaces)
       → Returns RouteDecision (structured routing plan)

3. Execute tools sequentially per tool_sequence
   └─ Executor dispatch
       ├─ native capability → ToolRegistry.execute(tool_name, **kwargs)
       └─ `external_mcp` capability → MCPAdapter.invoke(capability_name, json_safe_kwargs)
       ├─ Collect tool results
       ├─ Accumulate graph_paths and evidence_chunks (for path explanation)
       └─ Support early termination (optional tools skipped when preceding tools succeed)

3b. Execute local skill plan (optional)
   └─ When `route.skill_plan` is present:
       └─ SkillExecutor.execute(skill_name, goal, user_query, workspace, constraints)
            ├─ SkillLoader loads `SKILL.md` + file inventory on demand
            ├─ SkillCommandPlanner derives a canonical `SkillCommandPlan`
            └─ Runtime handoff stays behind the skill executor boundary and returns canonical `run_status`

3c. Explicit capability invocation (API / direct execution path)
   └─ AgentCore.invoke_capability(capability_name, session_id, query, args, ...)
       ├─ Bypasses RouteJudge and `tool_sequence`
       ├─ Reuses the same capability registry, memory-context loading, and executor dispatch
       └─ Returns one serialized capability result payload directly to the caller

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
       ├─ Candidate path scoring (lightweight semantic token expansion + node/edge phrase coverage + evidence support + hop penalty)
       ├─ Select best path + supporting evidence chunks
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
    async def plan(
        query,
        session_context,
        user_profile,
        available_capabilities,
        available_capability_catalog=None,
        available_skills=None,
    ) -> RouteDecision
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
| `skill_plan` | `SkillPlan \| None` | Optional local skill execution plan |
| `reason` | `str` | Routing decision rationale |
| `max_iterations` | `int` | Maximum execution steps |

### 6.3 Routing Logic (LLM + Rule Fallback)

The rule engine covers explicit patterns, local skill matching, planner-visible external capability matching, plus the default factual fallback:

| Scenario | Detection | Strategy | Tool Sequence |
|---|---|---|---|
| Simple greeting | `SIMPLE_PATTERN` | `simple_answer_no_tool` | Empty |
| KG ingestion | `INGEST_PATTERN` | `kg_ingest_request` | [web_search →] kg_ingest |
| Entity query | `ENTITY_PATTERN` | `graph_entity_lookup_first` | graph_entity_lookup → kg_hybrid_search |
| Relation/causal | `RELATION_PATTERN` | `kg_hybrid_first_then_graph_trace` | kg_hybrid_search → graph_relation_trace |
| Context follow-up | `FOLLOWUP_PATTERN` | `memory_first_then_*` | memory_search → subsequent tools |
| Local skill match | Lexical/metadata match against `available_skills` | `skill_request` or `memory_first_then_skill` | skill_plan |
| Planner-visible external capability match | Lexical/metadata match against `available_capability_catalog` | `external_capability_request` or `memory_first_then_external_capability` | [memory_search →] external capability |
| Real-time info | `REALTIME_PATTERN` | `freshness_aware_search` | web_search → kg_hybrid_search |
| User correction | `CORRECTION_PATTERN` + recent KG tools | `correction_and_refresh` | web_search |
| Direct URL query | `URL_PATTERN` | `direct_url_crawl` | web_search |
| Specialized local workflow (for example spreadsheet work) with matching skill | `SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN` or explicit workflow terms + skill match | `skill_request` | skill_plan |
| Specialized external analysis without a matching skill but with matching external capability | `SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN` + capability match | `external_capability_request` | external capability |
| Specialized external analysis without a matching skill/capability | `SPECIALIZED_EXTERNAL_CAPABILITY_PATTERN` | `specialized_external_capability` | Empty |
| Default factual QA | Fallback | `factual_qa` | kg_hybrid_search |

Skill-aware routing details:

- `AgentCore` now feeds `RouteJudge` two separate planner surfaces: a capability catalog and an independent local skill catalog
- The capability catalog summarizes `description`, `tags`, `kind`, `executor`, and top-level argument contract (`arg_names`, `required_args`) so planner-visible external MCP capabilities can be selected without hardcoded per-capability route rules
- The skill catalog is intentionally lightweight: `name`, `description`, optional `tags`, and local path. It does not require `default_script`, `recommended_script`, or `entrypoint`
- `RouteJudge` now emits `skill_plan` for local skills instead of auto-composing legacy helper-tool workflows
- Legacy helper APIs such as `list_skills`, `read_skill_docs`, and `execute_skill_script` may still exist for compatibility, but they are no longer the planner's primary skill surface

LLM refinement: When `llm_client` is available, the rule result is passed to the LLM for secondary adjustment using both the capability catalog and `available_skills`. The LLM remains constrained by those provided planner surfaces; it will not invent nonexistent capabilities or skills.

Route Judge prompt management now supports versioned templates through `agent/prompts.py`:

- `build_route_judge_prompt(..., prompt_version=...)` resolves the configured version before rendering
- `list_route_judge_prompt_versions()` exposes the registered versions
- unknown versions fall back to `v1` with a warning
- current built-in versions are:
  - `v1`: original concise route-refinement prompt, now also exposing capability catalog context
  - `v2`: more conservative refinement prompt that emphasizes minimal edits to the rule-based plan while explicitly using the capability catalog for planner-visible external skills

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

1. **Candidate path scoring:** lightweight semantic-expanded lexical scoring over query/path/evidence alignment, with node/edge phrase coverage, evidence support, and overlong-path penalties
2. **Evidence matching:** select the highest-support evidence chunks for the best path
3. **LLM explanation generation:** invoke LLM when available, otherwise fall back to template concatenation
4. **No-result fallback:** returns `enabled=False` when paths or evidence are insufficient

### 7.4 Design Notes

- Domain-agnostic module, not limited to economics (`domain_schema` is an optional parameter)
- `domain_schema.explanation_profile` is now the preferred source for path-explainer intent triggers, semantic tags, relation semantics, node-role rules, path constraints, evidence policies, output contracts, guardrails, prompt-template hints, and optional `scenario_overrides`; legacy regex/tag constants remain only as backward-compatible fallback when no profile is present
- For domain customization, prefer editing the profile section blocks in the lower-layer schema file (for example `lightrag_fork/schemas/general.py` and `lightrag_fork/schemas/economy.py`) rather than changing `path_explainer.py`; the upper-layer explainer is intended to stay generic and consume those fields declaratively
- `prompts.py` now resolves path-explainer templates through a registry (`path_base_v1`, intent-level templates, economy-domain templates); `explanation_profile.prompt_bindings` only selects `template_id`, while actual prompt text and fallback logic stay in the upper-layer registry
- When an `intent_binding.scenario_id` matches a declared `scenario_override`, Path Explainer resolves template/policy/contract/guardrail precedence as `scenario override -> intent binding -> profile intent/default fallback`
- `question_type` is still the external compatibility label (`relation_explanation` / `path_trace`), but internally the explainer can now resolve a richer `intent_family` before mapping back to that legacy field
- Path scoring now also consumes profile-declared `node_role_rules` and `path_constraints` to penalize overlong/repeated paths, reward role-aligned node sequences, and apply relation-direction/type consistency checks when the schema declares them
- Does not modify the graph; only responsible for explanation
- Does not blindly trust graph relationships; requires "graph connectivity + text support"
- `AgentCore` wires Path Explainer through the same utility-model client used by Route Judge, with one-time fallback to the main agent model when no dedicated utility model is configured

---

## 8. Tool System And Capability Layer

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
agent/capability_registry.py  build_native_capability_registry(tool_registry)
    └─ Mirror native ToolDefinition objects into planner-visible CapabilityDefinition entries
    ↓
config.py  MCPConfig.from_env()
    ├─ `KG_AGENT_MCP_SERVERS_JSON`
    └─ `KG_AGENT_MCP_CAPABILITIES_JSON`
    ↓
agent/agent_core.py  AgentCore.initialize_external_capabilities()
    ├─ `MCPAdapter.discover_capabilities()` optionally calls MCP `tools/list`
    ├─ Static MCP declarations override discovered tools with the same `(server, remote_name)`
    └─ Newly discovered external capabilities are registered into CapabilityRegistry
    ↓
agent/agent_core.py  AgentCore.__init__() holds ToolRegistry + CapabilityRegistry
    ↓
Planner / API discovery → CapabilityRegistry.list_capabilities()
    ↓
Runtime execution
    ├─ native → ToolRegistry.execute(name, **kwargs)
    └─ external_mcp → MCPAdapter.invoke(name, json_safe_kwargs)
```

### 8.3 Current Native Capability Inventory

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

> **Note:** "Tool registered" does not mean "enabled by default". `GET /agent/tools` only returns tools with `enabled=True`.
> `GET /agent/tools` now exposes built-in capability metadata derived from the core native registry, including `kind="native"` and `executor="tool_registry"`.
> When MCP capabilities are configured, `GET /agent/tools` also returns `kind="external_mcp"` / `executor="mcp"` entries, including dynamically discovered MCP tools after app startup or the first async agent call.
> At default startup, 7 tools are available when `user_id` is present (`web_search` is disabled by default; `cross_session_search` is user-scoped).

### 8.4 Skills Layer

- `SkillRegistry` scans `./skills/*/SKILL.md` and exposes a lightweight catalog to the planner
- `SkillLoader` supports progressive disclosure: load `SKILL.md` first, then read specific files on demand (`references/`, `scripts/`, `assets/`, other markdown)
- Skill execution stages are now explicit: `registry -> loader -> command planner -> executor -> runtime`
- `SkillCommandPlanner` is responsible for turning `LoadedSkill + SkillExecutionRequest` into a canonical `SkillCommandPlan`
- `SkillCommandPlan` is the only place where planned command details live: `command`, `mode`, `shell_mode`, `entrypoint`, `cli_args`, optional `generated_files`, optional `bootstrap_commands`, `bootstrap_reason`, `missing_fields`, `failure_reason`, `hints`
- `SkillExecutor` is the only skill execution entry in `AgentCore`; the planner no longer decomposes a skill into helper-tool chains
- The runtime target is now **shell-oriented skill execution inside an isolated workspace / VM / container**, not planner-visible remote script calls
- `SkillExecutionRequest` and `SkillCommandPlan` now both carry an explicit `runtime_target` with `platform`, `shell`, `workspace_root`, `workdir`, `network_allowed`, and `supports_python`
- The default runtime target is now **Linux-first**: `platform="linux"` and `shell="/bin/sh"` unless overridden by skill-runtime config
- Current default execution boundary is still conservative: the executor prepares the local shell-runtime context and can hand off to a runtime client, but it does not require `default_script` / `recommended_script` / `entrypoint` just to discover a skill
- Skill shell planning now supports two runtime-facing modes:
  - `shell_mode="conservative"`: current safe planner path (`explicit`, `structured_args`, `inferred`, or `manual_required`)
  - `shell_mode="free_shell"`: utility-LLM-assisted shell planning that, when explicitly requested, now tries the free-shell LLM path first and may synthesize richer shell commands or multi-file `generated_files` bundles before execution
- When `KG_AGENT_SKILL_RUNTIME_SERVER` is configured and `MCPAdapter` is available, `AgentCore` can auto-wire an internal `MCPBasedSkillRuntimeClient` so `skill_plan` execution uses MCP only as a transport layer behind `SkillExecutor`
- The MCP skill runtime server now shares the same `SkillCommandPlanner` surface; when `run_skill_task` receives `constraints.shell_mode="free_shell"` without an explicit `command_plan`, `mcp-server/server.py` can independently plan the shell task if a utility LLM is configured
- If no utility LLM is configured for that free-shell planning path, the runtime returns canonical `run_status="manual_required"` with `failure_reason="llm_not_available"` instead of falling back to duplicate server-local planning helpers
- Free-shell planning now gets a larger SKILL.md Python-example surface plus the conservative fallback plan as prompt context, so the planner can derive more complex commands from natural-language goals or dense script examples instead of only mirroring the conservative path
- The current coarse-grained runtime contract is shell-oriented: it accepts either an explicit shell task (`constraints.shell_command` / `constraints.command`) or structured CLI args (`constraints.args` / `constraints.cli_args`) when the skill has a single runnable entrypoint, and it returns run-oriented data such as `run_id`, `command`, `logs_preview`, `artifacts`, and canonical `run_status`
- In `free_shell` mode, `SkillCommandPlanner` may also return `generated_files`, which the runtime materializes inside the isolated workspace before executing the final shell command
- Generated-script plans no longer require a single inline `command`: the planner may instead return `generated_files + entrypoint + cli_args`, and the runtime will materialize the whole bundle, choose/validate the generated entrypoint, and then execute that entrypoint inside the isolated workspace
- Free-shell planning now also auto-promotes long inline `python -c ...` plans into `generated_script` bundles when the request/doc context indicates script-first execution or rich Python-example synthesis, so “write script first, then execute” no longer depends only on the LLM returning `generated_files` explicitly
- Free-shell planning can now also return `bootstrap_commands` plus `bootstrap_reason` when dependency/tool setup should happen before the main command; planner prompts steer Python package bootstrap toward workspace-local installs such as `python -m pip install --target ./.skill_bootstrap/site-packages ...`
- Before execution, the runtime now performs a `preflight` phase for planned shell runs:
  - validate `required_tools` from planner hints against the runtime PATH
  - validate each `generated_file` path stays relative to the declared runtime workspace and remains within size limits
  - run `py_compile` syntax checks for generated Python files when `runtime_target.supports_python=true`
- Preflight failures no longer hard-crash the runtime; they are returned as structured `preflight` results with canonical `run_status="manual_required"` or `failed`, plus a concrete `failure_reason`
- `free_shell` execution now has a bounded multi-step observe-execute-repair loop: after a failed execution or repairable preflight failure, the runtime may send the failed command, stdout/stderr, exit code, preflight result, prior failed attempts, and a skill-doc excerpt back to the utility LLM and retry with a repaired `SkillCommandPlan` up to the configured repair limit
- Repair metadata is now part of the canonical run record: `repair_attempted`, `repair_succeeded`, `repaired_from_run_id`, `repair_attempt_count`, `repair_attempt_limit`, and bounded `repair_history`
- The runtime now also has a bounded bootstrap/install lifecycle for shell skills: when a plan carries `bootstrap_commands`, it executes them inside the isolated workspace before the main command, exposes `.skill_bootstrap/bin`, `.skill_bootstrap/Scripts`, and `.skill_bootstrap/site-packages` through `PATH` / `PYTHONPATH`, then re-runs preflight and execution with bounded bootstrap metadata/history
- The runtime service executes those shell commands inside an isolated workspace, using `/bin/sh -lc` on POSIX runtimes and PowerShell on Windows for local development/testing
- For local development/testing, the runtime prepends the active interpreter directory to `PATH` so planned `python ...` shell commands resolve inside the current environment
- Canonical skill lifecycle is tracked via `run_status`: `planned`, `running`, `completed`, `failed`, `manual_required`
- Compatibility-only outward `status` is derived at the API / MCP boundary. Internal code must branch only on canonical `run_status`
- `manual_required` is the canonical “cannot safely auto-run yet” state; legacy `status="needs_shell_command"` is only one compatibility surface for it
- Live shell execution now uses a **durable queue-worker model** inside `mcp-server/server.py`: runtime-backed `run_skill_task` persists the run record plus worker job context to SQLite, ensures an independent queue-worker process is available, and returns `run_status="running"` once preflight passes
- Durable queue progress is surfaced through `runtime.queue_state` (`queued`, `claimed`, `executing`, `cancelling`, terminal states), while the public lifecycle contract still uses canonical `run_status`
- The durable queue now also tracks lease/attempt metadata in `runtime`: `lease_owner`, `lease_expires_at`, `attempt_count`, and `max_attempts`
- Queue workers are now configurable as a small local pool via `MCP_QUEUE_WORKER_CONCURRENCY`; stale claimed/executing runs may be re-queued until `max_attempts` is exhausted, after which the run terminates as `worker_lost`
- Running shell tasks now update `stdout` / `stderr` incrementally in the durable SQLite-backed run store, and the runtime also refreshes visible workspace artifacts while the task is still running
- Runtime-backed shell runs can now be cancelled through `cancel_skill_run(run_id)`; cancellation marks the durable run row, terminates the active shell process when possible, and resolves to canonical `run_status="failed"` with `failure_reason="cancelled"` plus `cancel_requested=true`
- For blocking or test-oriented flows, callers can still request synchronous completion with `wait_for_completion=true` (or `constraints.wait_for_completion=true`)
- `RouteJudge` now preserves and emits structured `skill_plan.constraints` when the user query makes them explicit; for example, spreadsheet queries can carry `input_path`, `operation="recalc"`, or `preserve_formulas=true`
- Route Judge also treats “pure shell / script-first” natural-language cues such as “write a helper script first”, “generate a script”, or `先写脚本再执行` as `skill_plan.constraints.shell_mode="free_shell"` so complex shell tasks can route into the free-shell planner without the user naming the mode literally
- When `RouteJudge` emits `skill_plan`, `AgentCore` runs it through `SkillExecutor` after any prerequisite tools (for example memory lookup) and before final answer generation
- Skill execution records are serialized back into the same run result stream / memory metadata surface as tool calls, with `metadata.executor="skill"`
- Skills also have an explicit API plane separate from capabilities: `GET /agent/skills`, `GET /agent/skills/{skill_name}`, `GET /agent/skills/{skill_name}/files/{relative_path}`, and `POST /agent/skills/{skill_name}/invoke`
- When a runtime-backed skill invocation returns a `run_id`, the skill plane also supports follow-up run inspection through `GET /agent/skill-runs/{run_id}`, `GET /agent/skill-runs/{run_id}/logs`, and `GET /agent/skill-runs/{run_id}/artifacts`
- `AgentCore.invoke_skill(...)` bypasses `RouteJudge` and executes one local skill directly through `SkillExecutor`; this still does not make the skill appear as a planner-visible MCP tool

### 8.5 MCP External Capability Layer

- `MCPAdapter` currently supports **stdio** transport only
- MCP capabilities can be provided in two ways:
  - statically declared through config (`KG_AGENT_MCP_CAPABILITIES_JSON`)
  - optionally discovered from MCP `tools/list` when a server entry sets `discover_tools=true`
- MCP capabilities can now be invoked explicitly through `AgentCore.invoke_capability(...)` and `POST /agent/capabilities/{capability_name}/invoke`, without constructing a synthetic `ToolCallPlan`
- When an external capability is `planner_exposed=true`, `AgentCore` now surfaces it to `RouteJudge` not only by name but also via the planner-facing capability catalog
- Dynamic discovery is additive and compatible with static declarations:
  - static declarations keep custom public names, `planner_exposed`, tags, and remote-name overrides
  - if a discovered remote tool matches a static declaration on the same `(server, remote_name)`, the static declaration wins and no duplicate capability is registered
  - newly discovered capabilities default to `planner_exposed=false`
- Each configured capability maps:
  - planner/API-visible `CapabilityDefinition.name`
  - target MCP server name
  - optional remote tool name override (`remote_name`)
  - `planner_exposed` flag controlling whether the Route Judge can see it by default
- External capability execution sanitizes arguments down to JSON-safe values before sending them across MCP, so in-process objects such as `rag`, stores, adapters, and locks are not leaked into the transport payload
- MCP is no longer the primary abstraction for local skills. A skill runtime service may still use MCP as an internal transport, but planner-visible skill selection comes from `SkillRegistry`, not from `list_skills` / `read_skill_docs` / `execute_skill_script`
- For generic skill-container workflows, `AgentCore` now supports additional binding transforms over prior tool results:
  - `selected_skill_name`
  - `skill_name`
  - `default_skill_script`
- These legacy transforms remain only as compatibility hooks for old helper-style flows; they are not part of the new planner-facing skill abstraction
- The current execution result shape stores:
  - `data.summary`
  - `data.structured_content`
  - `data.content`
  - `data.raw`
  - `metadata.executor="mcp"`
  - `metadata.server=<server_name>`

### 8.6 Crawler Layer Architecture

The crawler layer (`kg_agent/crawler/`) provides the infrastructure behind `web_search`:

```
web_search(query, urls, top_k)
  ├─ _collect_urls()                       # Extract URLs from query text + urls arg
  ├─ [no URLs] → crawler_adapter.discover_urls(query, top_k)
  │    ├─ build_search_url(engine, query)  # DuckDuckGo HTML URL
  │    ├─ crawl_url(search_url)            # Crawl search results page
  │    └─ extract_search_results_from_markdown(markdown, query, top_k)
  │         ├─ _decode_search_redirect()   # DuckDuckGo /l/?uddg= redirect decoding
  │         ├─ canonicalize_url() / _normalize_result_url()
  │         │    Strip tracking params, remove fragments, normalize host/path
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
- `MonitoredSource.content_lifecycle.resolved_content_class()` defaults crawler outputs to `short_term_news` for feed sources and `long_term_knowledge` for normal page sources unless explicitly overridden
- `MonitoredSource.content_lifecycle.resolved_update_mode()` defaults short-term sources to `replace_latest` and long-term sources to `append`
- `MonitoredSource.content_lifecycle.resolved_event_cluster_mode()` defaults short-term feed sources to heuristic event clustering and upgrades `auto` to `heuristic_llm` when the shared Utility LLM client is available
- `IngestScheduler` tracks `consecutive_no_change` in crawl state and increases the effective poll interval for feed sources after repeated no-change polls; a successful new ingest resets that feed backoff
- For feed sources, the scheduler now calls `discover_feed_urls()` first, canonicalizes feed entry URLs, drops duplicate aliases, applies `feed_filter.include_patterns` / `exclude_patterns`, optional author/category/domain constraints, and optional `feed_filter.max_age_days` against feed entry metadata, then applies `feed_priority` ranking across the discovered feed entries before sending the selected article URLs back through `crawler_adapter.crawl_urls()` so Crawl4AI batch crawling stays centralized in the adapter
- After crawl, feed pages also pass through `feed_dedup`: normalized content fingerprints are compared against retained feed items so mirrored URLs or low-increment update posts can be suppressed before `rag.ainsert()` writes another graph/vector document
- `DiscoveredUrl.published_at` is populated from RSS `pubDate` / Atom `updated` or `published` when available; `DiscoveredUrl.author` and `DiscoveredUrl.categories` are also extracted from RSS/Atom feed entry metadata when present
- `CrawlStateRecord.recent_item_keys` maintains feed item recency ordering, `item_published_at` stores per-item timestamps when known, `item_content_fingerprints` stores per-item normalized content signatures, `item_active_doc_ids` tracks the currently active crawler-inserted document per item, `item_event_cluster_ids` maps canonical feed items into event clusters, `event_clusters` stores the cluster records owned by that source, and `doc_expires_at` tracks when superseded or short-lived documents should be considered expired
- When event clustering is enabled for short-term feed sources, newly crawled articles first score against local cluster metadata and can also query the workspace-global `chunks_vdb` to find active short-term event clusters created by other feed sources in the same workspace; borderline candidates can optionally be adjudicated by the shared Utility LLM client already configured for `AgentCore`
- When an expired/deleted short-term document was also referenced as the active doc of a cluster owned by another source, the scheduler now clears that cross-source active reference so retrieval filtering cannot accidentally resurrect stale news through the global cluster pointer
- For short-term lifecycle sources, `IngestScheduler` now passes deterministic `ids=` into `rag.ainsert()` when the underlying RAG supports it, which lets the scheduler later call `rag.adelete_by_doc_id()` for optional expiry cleanup without touching `lightrag_fork`
- Scheduler loop maintenance now also performs an expired-document sweep independent of normal crawl polling, so TTL-based cleanup is not blocked behind the source's next ingest interval
- Removing a short-term lifecycle source now also attempts to delete all crawler-managed documents for that source immediately; if the active RAG backend cannot delete by doc ID, the scheduler keeps a tombstone-style state record so retrieval filtering still suppresses those orphaned short-term docs
- `feed_retention.mode="latest"` / `feed_retention.max_age_days` still prune tracked feed items from scheduler state; when short-term lifecycle is enabled, items pruned out of the retained set can also mark their previously active crawler documents as expired
- `CrawlerConfig` now also supports optional Crawl4AI `LLMExtractionStrategy`; when enabled, the adapter still keeps Crawl4AI's native markdown path by default, but `llm_extraction_prefer_content=true` can replace page markdown with the LLM-extracted payload before downstream tools or scheduler ingestion consume it

**Key content_extractor constants:**

| Constant | Purpose |
|---|---|
| `BLOCKED_RESULT_DOMAINS` | Domains always excluded from discovery (social media, etc.) |
| `LOW_QUALITY_RESULT_DOMAINS` | Domains deprioritized in ranking |
| `GENERIC_PATH_SEGMENTS` | URL path segments indicating listing pages (tag, category, archive, etc.) |
| `TRACKING_QUERY_KEYS` | URL query parameters stripped during normalization (utm_*, ref, src, etc.) |

### 8.7 Web Search Semantics

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

### 8.8 Standard Steps for Adding a New Tool

1. Create a new file under `tools/`, implementing `async def my_tool(*, rag, query, ..., **_) -> ToolResult`
2. Add parameter JSON Schema in `tool_schemas.py`
3. Register in `build_default_tool_registry()` in `builtin_tools.py`
4. If toggle control is needed, add a corresponding field in `ToolConfig` in `config.py`
5. If routing support is needed, add a corresponding rule pattern in `route_judge.py`

### 8.9 Retrieval Freshness Decay

- `kg_hybrid_search` and `kg_naive_search` both accept internal `search_query`
- When `KG_AGENT_ENABLE_FRESHNESS_DECAY=true`, `retrieval_tools.py` passes freshness parameters into `QueryParam`
- Lower-layer freshness-aware ranking now runs inside `lightrag_fork/operate.py`
- `retrieval_tools.py` only keeps a compatibility fallback when lower-layer metadata does not mark `freshness_decay_applied`
- When `AgentCore` is attached to a scheduler state store, retrieval tools now also reverse-map `chunk_id` / graph `source_id` back to `full_doc_id` via `rag.text_chunks` and suppress crawler-managed short-term results whose documents are expired or no longer the active version for that tracked item
- Retrieval-side crawler lifecycle filtering rebuilds chunk references after filtering so the final answer prompt and debug tool payload only expose still-active evidence chunks

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

### 10.2 MCP Configuration (MCPConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_MCP_SERVERS_JSON` | `[]` | JSON array of stdio MCP server definitions (`name`, `command`, optional `args`, `env`, `startup_timeout_s`, `tool_timeout_s`, optional `discover_tools`) |
| `KG_AGENT_MCP_CAPABILITIES_JSON` | `[]` | JSON array of static MCP capability declarations (`name`, `description`, `server`, `input_schema`, optional `remote_name`, `planner_exposed`, `tags`, `enabled`) |

### 10.3 Tool Switches (ToolConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_ENABLE_WEB_SEARCH` | `false` | Enable web search |
| `KG_AGENT_ENABLE_MEMORY` | `true` | Enable conversation memory |
| `KG_AGENT_ENABLE_KG_INGEST` | `true` | Enable KG ingestion tool |

### 10.4 Runtime Configuration (AgentRuntimeConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_DEFAULT_WORKSPACE` | Falls back to `WORKSPACE` | Default workspace |
| `KG_AGENT_DEFAULT_DOMAIN_SCHEMA` | `general` | Default domain schema |
| `KG_AGENT_SKILLS_DIR` | `skills` | Local skill directory scanned by `SkillRegistry` |
| `KG_AGENT_SKILL_RUNTIME_SERVER` | empty | Optional MCP server name used only as the internal skill-runtime transport |
| `KG_AGENT_SKILL_RUNTIME_RUN_TOOL` | `run_skill_task` | Remote coarse-grained shell runtime tool used by `MCPBasedSkillRuntimeClient` |
| `KG_AGENT_SKILL_RUNTIME_STATUS_TOOL` | `get_run_status` | Optional remote run-status retrieval tool name reserved for shell-based skill runs |
| `KG_AGENT_SKILL_RUNTIME_READ_TOOL` | `read_skill` | Reserved remote read interface for coarse-grained runtime access |
| `KG_AGENT_SKILL_RUNTIME_READ_FILE_TOOL` | `read_skill_file` | Reserved remote file-read interface for coarse-grained runtime access |
| `KG_AGENT_SKILL_RUNTIME_LOGS_TOOL` | `get_run_logs` | Optional remote run-log retrieval tool name reserved for shell-based skill runs |
| `KG_AGENT_SKILL_RUNTIME_ARTIFACTS_TOOL` | `get_run_artifacts` | Optional remote artifact listing tool name reserved for shell-based skill runs |
| `KG_AGENT_SKILL_DEFAULT_SHELL_MODE` | `conservative` | Default skill shell-planning mode: `conservative` or `free_shell` |
| `KG_AGENT_SKILL_TARGET_PLATFORM` | `linux` | Default skill runtime target platform (`linux` or `windows`) |
| `KG_AGENT_SKILL_TARGET_SHELL` | `/bin/sh` | Default skill runtime target shell (`/bin/sh`, `bash`, or `powershell`) |
| `KG_AGENT_SKILL_TARGET_WORKSPACE_ROOT` | `/workspace` | Default declared runtime workspace root passed into skill planning |
| `KG_AGENT_SKILL_TARGET_WORKDIR` | `/workspace` | Default declared runtime working directory passed into skill planning |
| `KG_AGENT_SKILL_TARGET_NETWORK_ALLOWED` | `false` | Whether free-shell planning may assume outbound network access by default |
| `KG_AGENT_SKILL_TARGET_SUPPORTS_PYTHON` | `true` | Whether the default runtime target may execute Python or generated Python helpers |
| `MCP_RUN_STORE_SQLITE_PATH` | `${MCP_WORKSPACE_DIR}/skill_runtime_runs.sqlite3` | SQLite file used by the durable skill-runtime queue/store inside `mcp-server/server.py` |
| `MCP_QUEUE_WORKER_CONCURRENCY` | `1` | Number of local queue-worker processes the runtime tries to keep available for durable shell runs |
| `MCP_QUEUE_LEASE_TIMEOUT_S` | `45` | Lease expiration window used for claimed/executing durable shell runs |
| `MCP_QUEUE_MAX_ATTEMPTS` | `2` | Maximum durable worker execution attempts before a stale/lost run becomes terminal `worker_lost` |
| `KG_AGENT_MAX_ITERATIONS` | `3` | Maximum tool invocation rounds |
| `KG_AGENT_ROUTE_JUDGE_PROMPT_VERSION` | `v1` | Version key for Route Judge LLM refinement prompt templates |
| `KG_AGENT_MEMORY_WINDOW_TURNS` | `6` | Maximum number of conversation turns considered for the dynamic attention window |
| `KG_AGENT_MEMORY_MIN_RECENT_TURNS` | `2` | Minimum number of newest turns always kept in the dynamic attention window |
| `KG_AGENT_MEMORY_MAX_CONTEXT_TOKENS` | `1200` | Soft token budget used when selecting conversation history for routing/final answer prompts |
| `KG_AGENT_DEBUG` | `false` | Debug mode |

### 10.5 Crawler Configuration (CrawlerConfig)

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
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_ENABLED` | `false` | Enable Crawl4AI `LLMExtractionStrategy` during crawl |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_PROVIDER` | inferred `openai/<model>` or empty | Crawl4AI/LiteLLM provider string passed to `LLMConfig` |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_API_TOKEN` | falls back to `LLM_BINDING_API_KEY` / `OPENAI_API_KEY` | API token for Crawl4AI LLM extraction |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_BASE_URL` | falls back to `LLM_BINDING_HOST` / `OPENAI_API_BASE` | Base URL for Crawl4AI LLM extraction |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_INSTRUCTION` | empty | Optional extraction instruction prompt |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_INPUT_FORMAT` | `markdown` | Extraction input format (`markdown`, `html`, `fit_markdown`, etc.) |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_TYPE` | `block` | Crawl4AI extraction type (`block` or `schema`) |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_SCHEMA_JSON` | empty | Optional JSON schema string for schema-mode extraction |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_FORCE_JSON_RESPONSE` | `false` | Force JSON responses from the extraction LLM |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_APPLY_CHUNKING` | `true` | Let Crawl4AI chunk extraction input before LLM calls |
| `KG_AGENT_WEB_CRAWLER_LLM_EXTRACTION_PREFER_CONTENT` | `false` | Replace downstream page markdown with `result.extracted_content` when available |

### 10.6 Scheduler Configuration (SchedulerConfig)

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
- `feed_dedup.mode`: `auto`, `off`, `content_hash`, or `content_signature`; `auto` currently resolves to `content_signature`
- `feed_dedup.signature_token_limit`: token cap used by `content_signature`; only the leading normalized content tokens contribute to the duplicate fingerprint so tiny tail updates can still collapse
- `content_lifecycle.content_class`: `auto`, `long_term_knowledge`, or `short_term_news`; `auto` resolves from `source_type`
- `content_lifecycle.update_mode`: `auto`, `append`, or `replace_latest`; `auto` resolves to `replace_latest` for short-term content and `append` otherwise
- `content_lifecycle.ttl_days`: optional expiration horizon for active short-term documents; when positive, the scheduler tracks document expiry timestamps even if no newer version has arrived yet
- `content_lifecycle.delete_expired`: when `true`, the scheduler attempts `rag.adelete_by_doc_id()` for expired short-term crawler documents; when `false`, expiry is tracked in state only
- `content_lifecycle.event_cluster_mode`: `auto`, `off`, `heuristic`, or `heuristic_llm`; `auto` resolves to `heuristic_llm` only when the shared Utility LLM client is available, otherwise `heuristic`
- `content_lifecycle.event_cluster_window_days`: maximum publish-time gap considered when matching a new article into an existing event cluster; older clusters are ignored as unrelated followups
- `content_lifecycle.event_cluster_min_similarity`: minimum heuristic similarity required before a new article is merged into an existing event cluster without LLM adjudication

### 10.7 Persistence Configuration (PersistenceConfig)

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

### 10.8 Freshness Configuration (FreshnessConfig)

| Environment Variable | Default | Description |
|---|---|---|
| `KG_AGENT_FRESHNESS_THRESHOLD_SECONDS` | `604800` | Freshness threshold for graph age checks |
| `KG_AGENT_ENABLE_AUTO_INGEST` | `false` | Allow realtime auto-ingest bridge during chat |
| `KG_AGENT_STALENESS_DECAY_DAYS` | `7.0` | Half-life parameter for freshness-aware KG retrieval decay |
| `KG_AGENT_ENABLE_FRESHNESS_DECAY` | `false` | Enable freshness-aware KG retrieval decay |

### 10.9 Rerank Configuration

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
| GET | `/agent/skills` | `agent_routes.py` | List the local skill catalog from `SkillRegistry` |
| GET | `/agent/skills/{skill_name}` | `agent_routes.py` | Read one skill's `SKILL.md` plus file inventory |
| GET | `/agent/skills/{skill_name}/files/{relative_path}` | `agent_routes.py` | Read one file inside a skill directory on demand |
| POST | `/agent/skills/{skill_name}/invoke` | `agent_routes.py` | Explicitly execute one local skill through `SkillExecutor` |
| GET | `/agent/skill-runs/{run_id}` | `agent_routes.py` | Read lifecycle status for one runtime-backed shell skill run |
| GET | `/agent/skill-runs/{run_id}/logs` | `agent_routes.py` | Read logs for one runtime-backed shell skill run |
| GET | `/agent/skill-runs/{run_id}/artifacts` | `agent_routes.py` | Read artifact metadata for one runtime-backed shell skill run |
| POST | `/agent/capabilities/{capability_name}/invoke` | `agent_routes.py` | Explicitly invoke one native or external capability without going through RouteJudge |
| POST | `/agent/ingest` | `agent_routes.py` | Insert content into KG (web page, PDF, manual text, etc.) |
| GET | `/agent/tools` | `agent_routes.py` | View currently enabled capabilities, including configured and dynamically discovered external MCP capabilities |
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

### 11.2 Skills API

- `GET /agent/skills`
  - Returns the lightweight local skill catalog (`name`, `description`, `tags`, `path`) produced by `SkillRegistry`
- `GET /agent/skills/{skill_name}`
  - Returns one loaded skill payload: public skill metadata, raw `SKILL.md`, and the current file inventory discovered by `SkillLoader`
- `GET /agent/skills/{skill_name}/files/{relative_path}`
  - Reads one specific file inside the skill directory; path traversal outside the skill root is rejected
- `POST /agent/skills/{skill_name}/invoke`
  - Bypasses `RouteJudge` and executes exactly one local skill through `AgentCore.invoke_skill(...)`
  - Request body includes `session_id`, `goal`, optional `query`, optional `workspace`, and optional `constraints`
  - Execution path is now explicit: `SkillLoader -> SkillCommandPlanner -> runtime`
  - `SkillCommandPlanner` returns a canonical `SkillCommandPlan` before runtime handoff; the command planner may return `mode="manual_required"` instead of guessing a command
  - For shell-oriented runtime execution, either pass a coarse-grained command through `constraints.shell_command` (or `constraints.command`), or pass structured CLI args through `constraints.args` / `constraints.cli_args` when the skill has a single runnable entrypoint
  - You can also pass `constraints.shell_mode="free_shell"` to enable utility-LLM-assisted shell planning for complex skills; that mode now prefers the free-shell LLM planner when explicitly requested, may synthesize `generated_files`, and can execute a generated script bundle through `entrypoint + cli_args` inside the isolated runtime workspace
  - You can also pass `constraints.dry_run=true` (or `plan_only=true`) to ask the runtime to return the planned shell command without executing it
  - Runtime-backed execution now starts asynchronously by default: after preflight succeeds, the invoke response may return `run_status="running"` and the client should poll `/agent/skill-runs/{run_id}` for terminal state
  - If a caller explicitly needs a blocking result, pass `constraints.wait_for_completion=true`
  - The runtime service executes the resulting command inside the isolated skill workspace instead of exposing per-script MCP tools
  - Response includes the public skill descriptor, one serialized `skill:<name>` execution record, and invocation metadata; when a runtime server is attached, the result can also include `run_id`, canonical `run_status`, compatibility `status`, `command_plan`, `command`, `shell_mode`, `runtime_target`, `preflight`, repair metadata/history, bootstrap metadata/history, `logs_preview`, and `artifacts`
  - Explicit skill invocation remains on the skill plane; it does not go through `CapabilityRegistry`
- `GET /agent/skill-runs/{run_id}`
  - Returns lifecycle state for one runtime-backed skill run
  - Payload includes canonical `run_status`, compatibility `status`, `command_plan`, `command`, `shell_mode`, `runtime_target`, runtime delivery metadata (including queue state / lease / attempts), `preflight`, repair metadata, cancellation metadata, timing fields, exit code, and failure reason when available
- `POST /agent/skill-runs/{run_id}/cancel`
  - Requests cancellation for one runtime-backed skill run through the skill runtime client
  - For the current durable-worker shell runtime, this marks the SQLite-backed run row as cancel-requested, attempts to kill the active shell subprocess, and then returns the updated run status payload
  - Cancellation is represented as canonical `run_status="failed"` with `failure_reason="cancelled"` and `cancel_requested=true`
- `GET /agent/skill-runs/{run_id}/logs`
  - Returns the stdout/stderr payload for a previously started runtime-backed skill run, plus canonical `run_status`, `shell_mode`, `runtime_target`, `preflight`, repair metadata, and cancellation metadata
  - For the current durable-worker shell runtime, this endpoint returns incrementally accumulated logs while `run_status="running"`
  - This endpoint requires a configured skill runtime client; otherwise the API returns service unavailable
- `GET /agent/skill-runs/{run_id}/artifacts`
  - Returns the visible workspace artifact list for a previously started runtime-backed skill run, including incremental artifact refresh during `running` for the current durable-worker shell runtime
  - Returns the isolated workspace path plus produced artifact metadata for a previously started runtime-backed skill run, plus canonical `run_status`, `shell_mode`, `runtime_target`, `preflight`, and repair metadata
  - This endpoint also stays on the skill plane and does not involve `CapabilityRegistry`

### 11.3 POST /agent/capabilities/{capability_name}/invoke

**Request Body (CapabilityInvokeRequest):**

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | `str` | Yes | Session ID used for optional context loading |
| `query` | `str` | No | Free-form request text; defaults to empty string for purely structured capability calls |
| `user_id` | `str` | No | User ID |
| `workspace` | `str` | No | Workspace |
| `domain_schema` | `str \| dict` | No | Domain schema |
| `use_memory` | `bool` | No | Whether to load conversation memory into the execution context (default false) |
| `args` | `dict` | No | Structured capability arguments merged into the execution kwargs after reserved core fields are protected |

**Response Body (CapabilityInvokeResponse):**

| Field | Type | Description |
|---|---|---|
| `capability` | `dict` | Public capability metadata (`name`, `description`, `input_schema`, `kind`, `executor`, etc.) |
| `result` | `dict` | Serialized single capability execution record (`tool`, `success`, `summary`, `error`, `metadata`, `data`) |
| `metadata` | `dict` | Invocation metadata such as `session_id`, `workspace`, `use_memory`, `kind`, and `executor` |

**Behavior notes:**

- This endpoint bypasses RouteJudge planning and executes exactly one capability
- It works for both native capabilities and configured `external_mcp` capabilities
- Unlike `/agent/chat`, the returned capability result always includes the full execution payload rather than the pruned non-debug tool view
- Memory can be loaded for context, but explicit invocation does not persist a new user/assistant turn into conversation memory

### 11.4 POST /agent/ingest

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

### 11.5 Scheduler and Source APIs

- `GET /agent/scheduler/status`
  - Returns scheduler configured/enabled/running flags, persistence file paths, loop counters, and per-source status including resolved source type / schedule mode / feed priority mode / feed dedup mode / content class / update mode, `consecutive_no_change`, `tracked_item_count`, `active_doc_count`, `expired_doc_count`, and `effective_interval_seconds`
- `GET /agent/sources`
  - Returns all monitored sources, including `source_type`, `schedule_mode`, `feed_filter`, `feed_retention`, `feed_priority`, `feed_dedup`, `content_lifecycle`, `resolved_source_type`, `resolved_schedule_mode`, `resolved_feed_priority_mode`, `resolved_feed_dedup_mode`, `resolved_content_class`, and `resolved_update_mode`
- `POST /agent/sources`
  - Upserts a `MonitoredSource`; request payload accepts `source_type`, `schedule_mode`, `feed_filter`, `feed_retention`, `feed_priority`, `feed_dedup`, and `content_lifecycle`
- `DELETE /agent/sources/{source_id}`
  - Removes a source and its state record
- `POST /agent/sources/{source_id}/trigger`
  - Immediately polls a source and returns crawl/ingest counts, including `feed_deduplicated_count`, `superseded_count`, `expired_doc_count`, and `deleted_doc_count` when short-term lifecycle handling is active

### 11.6 GET /health

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
| `mcp_configured` | `bool` | Whether an MCP adapter was attached to the app |
| `mcp_capability_count` | `int` | Number of enabled external MCP capabilities currently registered in the app, including dynamically discovered ones |

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

Key chain-level regression tests in this repo:

- `tests/kg_agent/test_feed_scheduler_chain.py` covers `feed -> scheduler -> crawl_urls -> markdown -> rag.ainsert`
- `tests/kg_agent/test_web_ingest_chain.py` covers `web_search -> kg_ingest -> rag.ainsert`
- `tests/kg_agent/test_query_agent_answer_chain.py` covers `query -> AgentCore -> kg_hybrid_search -> LLM final answer`
- `tests/kg_agent/test_crawl4ai.py` is a raw-HTML Crawl4AI smoke test; it reuses the adapter's browser-channel selection logic and skips when neither a Playwright runtime nor a system Chrome/Edge browser is available

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
- [ ] If the feature is planner-visible, is it also represented in `CapabilityRegistry` with the correct `kind` / `executor` metadata?
- [ ] Does the new tool handler follow the `async def tool(*, rag, query, ..., **_) -> ToolResult` signature?
- [ ] Do new Route Judge rules only reference names present in `available_capabilities`?
- [ ] Do new environment variables have sensible defaults?
- [ ] Does it depend on LangChain or other heavy frameworks? (Forbidden)
- [ ] Does path explanation retain a fallback path?

### 13.2 Common Modification Patterns

| Scenario | Recommended Approach |
|---|---|
| Add a new native capability | Create file in `tools/` → add schema in `tool_schemas.py` → register in `builtin_tools.py` → ensure it appears in the mirrored native `CapabilityRegistry` |
| Modify routing strategy | Add regex pattern in `route_judge.py` or adjust LLM prompt |
| Improve path explanation | Adjust scoring/evidence selection logic in `path_explainer.py` |
| Extend search-engine discovery | Add engine support in `content_extractor.build_search_url()` and adapter |
| Integrate domain-specific external capability | Add MCP server + capability config, wire any needed transport support in `mcp/adapter.py`, and keep `planner_exposed=false` unless explicit auto-routing is intended |
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
| `CapabilityDefinition` | `agent/capability_registry.py` | Planner/API-visible capability metadata |
| `CapabilityRegistry` | `agent/capability_registry.py` | Registry for built-in and future external capabilities |
| `RouteJudge` | `agent/route_judge.py` | Route Judge: `.plan()` returns RouteDecision |
| `RouteDecision` | `agent/route_judge.py` | Structured routing plan |
| `ToolCallPlan` | `agent/route_judge.py` | Single-step tool invocation plan |
| `SkillRegistry` | `skills/registry.py` | Scan local `skills/*/SKILL.md` into a lightweight catalog |
| `SkillLoader` | `skills/loader.py` | Load `SKILL.md` and read skill files on demand |
| `SkillCommandPlanner` | `skills/command_planner.py` | Convert `LoadedSkill + SkillExecutionRequest` into `SkillCommandPlan` |
| `SkillExecutor` | `skills/executor.py` | Execute `skill_plan` through the local skill-runtime boundary |
| `SkillPlan` | `skills/models.py` | High-level local skill execution plan |
| `SkillCommandPlan` | `skills/models.py` | Canonical shell-command planning result for one skill request |
| `SkillRuntimeTarget` | `skills/models.py` | Explicit runtime target/environment declaration carried through planning and execution |
| `SkillRunRecord` | `skills/models.py` | Canonical skill run lifecycle record (`run_status`, command plan, timing, artifacts) |
| `kg_agent.agent` package exports | `agent/__init__.py` | Lazy public re-exports used to keep prompt/planner imports cycle-safe |
| `MCPBasedSkillRuntimeClient` | `skills/runtime_client.py` | Optional MCP-backed transport used internally by `SkillExecutor` |
| `PathExplainer` | `agent/path_explainer.py` | Path Explainer: `.explain()` returns PathExplanation |
| `PathExplanation` | `agent/path_explainer.py` | Path explanation result |
| `ExplainedPath` | `agent/path_explainer.py` | Single explained path |
| `ToolRegistry` | `agent/tool_registry.py` | Tool registration and execution |
| `ToolDefinition` | `tools/base.py` | Tool definition data structure |
| `ToolResult` | `tools/base.py` | Tool execution result |
| `kg_ingest()` | `tools/kg_ingest.py` | Generic KG ingestion tool |
| `build_native_capability_registry()` | `agent/capability_registry.py` | Mirror native tools into the capability layer |
| `add_mcp_capabilities()` | `agent/capability_registry.py` | Register static or dynamically discovered external MCP capabilities into the capability layer |
| `build_default_tool_registry()` | `agent/builtin_tools.py` | Build default tool set |
| `MCPAdapter` | `mcp/adapter.py` | stdio MCP execution backend for external capabilities |
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

- **MCP integration is currently stdio-only**; optional dynamic `tools/list` discovery now exists, but there is still no SSE/HTTP transport or richer MCP server capability negotiation yet
- **Configured `external_mcp` capabilities are hidden from auto-routing by default**; they execute correctly when explicitly invoked, and the Route Judge only sees them when `planner_exposed=true`
- **Local skill execution is now architecturally separate from MCP and oriented around shell execution inside an isolated runtime**; `SkillExecutor` now has an explicit command-planning stage, canonical `run_status`, LLM-first `free_shell` planning when requested, generated-script bundle execution through `entrypoint + cli_args`, and a bounded workspace-local bootstrap/install lifecycle through `bootstrap_commands`, but it still does not implement a full multi-step autonomous shell agent or a richer environment-provisioning system
- **Compatibility `status` is still exposed at the API / MCP boundary** for old clients (`manual_required -> needs_shell_command`), but internal logic must treat canonical `run_status` as the only source of truth
- **Runtime-backed shell runs now use an independent queue-worker process plus a SQLite-backed durable queue/store**, so queued/running state and live-polled logs/artifacts survive MCP server restarts as long as the SQLite file remains available and queue workers can be relaunched
- **Skill runtime run persistence is now SQLite-backed inside `mcp-server/server.py`**, but it is still local-file durability only; there is no distributed queue, multi-node worker pool, or conversation-memory linkage yet. Replacing it with a shared Redis/Postgres-style durable queue remains an explicit lower-priority TODO after the current local-runtime hardening work
- **Queue workers are still local process-pool workers on one host**; there is no cross-node worker coordination yet
- **`worker_lost` recovery intentionally stops at bounded requeue via `max_attempts`**; there is still no richer backoff policy or dead-letter queue flow
- **Running shell tasks now expose incremental polled logs/artifacts and explicit cancellation through the durable store**, with local lease/attempt bookkeeping and limited requeue on worker loss, but this is still polling rather than SSE log streaming or a richer external job-control plane
- **The new `free_shell` repair loop is intentionally bounded by a small retry limit rather than being open-ended**; it now supports multi-step observe-execute-repair (including repairable preflight failures), and the runtime also supports bounded workspace-local bootstrap commands, but there is still no unlimited autonomous shell loop, richer environment provisioning, or full host/container dependency management lifecycle
- **`mcp-server/server.py` no longer owns a duplicate free-shell planner**, but compatibility wrappers such as `read_skill_docs` / `execute_skill_script` still exist for older callers
- **Legacy script-level skill helper APIs still exist as a compatibility layer**; `read_skill_docs` / `execute_skill_script` and related binding transforms remain for old flows, but they are no longer the planner's primary skill surface
- **Scheduler** now supports optional loop leader election plus source-level coordination, but it still does not implement scheduler sharding or a richer distributed control plane
- **Source persistence** now supports `json` and `sqlite`, but there is still no Redis/Mongo/Postgres-backed scheduler registry/state implementation
- **RSS / Feed source support** now includes auto-detected feed typing, `adaptive_feed` scheduling, entry filtering, RSS/Atom `published_at` extraction, author/category/domain-aware filtering, canonical URL tracking, content-level dedup, short-term vs long-term crawler lifecycle classification, optional expiry deletion, workspace-global event-cluster candidate recall through `chunks_vdb`, batch article crawling via the shared crawler adapter, and both latest-item and age-based retention, but discovery/management is still URL-centric and does not yet provide richer feed semantics such as source credibility scoring or full feed graph provenance
- **Update-aware provenance / delta ingest** is still only partially implemented; the scheduler now tracks active crawler document IDs and can supersede or delete short-term documents, but it still cannot persist version-aware provenance histories or ingest only semantic deltas
- **Similar-news consolidation** now supports cross-source candidate recall inside the same workspace by querying `chunks_vdb` for active short-term event docs and then merging into the nearest existing cluster with heuristic scoring plus optional Utility-LLM adjudication for borderline cases, but it is still not a fully materialized global event graph and does not maintain long-lived multi-version provenance inside each cluster
- **Crawler lifecycle retrieval filtering** now suppresses expired or superseded short-term crawler documents even when `content_lifecycle.delete_expired=false`, but the filtering path still depends on `chunk_id` / graph `source_id` being available and reverse-resolvable through `rag.text_chunks`; if lower-layer result metadata is missing or inconsistent, filtering falls back to best effort
- **Removed-source tombstones** are a pragmatic fallback for backends that cannot delete by doc ID; they preserve retrieval suppression for short-term docs after source removal, but they are not a full archival/provenance model and are reset if the same `source_id` is later re-added
- **Freshness decay** now has lower-layer support through `QueryParam.enable_freshness_decay` and `lightrag_fork/operate.py`, while retrieval-layer fallback remains for compatibility
- **Playwright browser detection** uses Playwright sync API to verify the exact chromium executable exists; on version mismatch the adapter falls back to system Edge/Chrome via `browser_channel`
- **CrossSessionStore** now supports an optional Mongo + Qdrant vector backend with heuristic compression and dedup, but it still falls back to conversation-memory token overlap when the vector backend is disabled or unavailable
- Cross-session consolidation and background aging are heuristic only; they use vector similarity plus lightweight lexical guards and snippet packing, not LLM summarization or a full long-horizon memory graph
- Path explanation now prefers schema-provided `explanation_profile` contracts when available and already consumes relation semantics, node-role rules, path constraints, evidence policies, output contracts, guardrails, prompt-template IDs, and first-class `scenario_overrides`, but only `general` and `economy` built-in profiles exist today and broader domain/scenario libraries are not yet materialized
- Path explanation now uses lightweight semantic-expanded lexical scoring; embedding-based semantic reranking is still not integrated
