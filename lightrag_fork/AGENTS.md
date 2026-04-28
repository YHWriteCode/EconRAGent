# AGENTS.md — lightrag_fork

> This file is for AI coding assistants (Copilot / Cursor / Cline, etc.) to reference when modifying code in `lightrag_fork/`.
> Human developers can also use it as an architecture quick-reference.

---

## 1. Module Positioning

`lightrag_fork/` is the **graph + vector backend layer** of this project, based on the open-source project [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) with minimally invasive enhancements.

**Core Responsibilities:**

- Document chunking, entity/relation extraction, graph merging, vector writes
- Multi-backend storage management (KV / Vector / Graph / DocStatus)
- Hybrid retrieval (naive / local / global / hybrid / mix)
- Concurrency control (local locks / Redis distributed locks)
- Optional domain schema prompt injection
- Temporal metadata maintenance for graph nodes/edges (`created_at`, `last_confirmed_at`, `confirmation_count`) and retrieval result passthrough
- Chunk provenance metadata propagation (`source_label`, page/section/file/crawler metadata) while preserving `file_path` as the backward-compatible citation field

**Not Responsible For:**

- Agent orchestration, tool invocation, conversation memory, user profiles → handled by the upper layer `kg_agent/`
- Frontend display, business API routes → handled by the upper layer's unified API or frontend

---

## 2. Directory Structure and File Responsibilities

```
lightrag_fork/
├── __init__.py              # Package entry, exports LightRAG, QueryParam
├── lightrag.py              # Main orchestration entry: initialization, storage init, document insertion, query entry
├── operate.py               # Core execution: chunk splitting, entity extraction, graph merging, vector writes, retrieval
├── base.py                  # Four storage abstract interfaces (KV / Vector / Graph / DocStatus)
├── prompt.py                # Static prompt templates (entity_extraction, query, etc.)
├── schema.py                # Domain schema config structure, built-in schema parsing and normalization
├── constants.py             # Centralized default constants (extraction params, query params, schema defaults)
├── namespace.py             # NameSpace constants: key rules for KV / Vector / Graph / DocStatus
├── types.py                 # Pydantic data models (KnowledgeGraph, Node, Edge, etc.)
├── exceptions.py            # Custom exceptions (API status codes, connection errors, pipeline exceptions, etc.)
├── rerank.py                # Reranker: document chunking + external rerank API calls
├── utils.py                 # General utility functions (tokenizer, hash, logging, caching, etc.)
├── utils_graph.py           # Graph operation utilities (node/relation CRUD, persistence callbacks)
│
├── schemas/                 # Built-in domain schema definitions
│   ├── __init__.py          # Exports GENERAL_DOMAIN_SCHEMA, ECONOMY_DOMAIN_SCHEMA
│   ├── general.py           # Default general schema (preserves original LightRAG behavior)
│   └── economy.py           # Economy domain schema (Company/Industry/Policy/Metric, etc.)
│
├── kg/                      # Storage backend implementations
│   ├── __init__.py          # STORAGES registry, storage factory
│   ├── shared_storage.py    # Shared lock management: KeyedUnifiedLock, NamespaceLock, pipeline locks
│   ├── lock_backend.py      # Lock abstraction: LockBackend / LocalLockBackend / LockLease
│   ├── redis_lock_backend.py# Redis distributed lock: RedisLockManager / RedisLockBackend
│   ├── neo4j_impl.py        # Neo4j graph storage backend
│   ├── qdrant_impl.py       # Qdrant vector storage backend
│   ├── mongo_impl.py        # MongoDB KV/Vector/DocStatus storage backend
│   ├── redis_impl.py        # Redis KV/DocStatus storage backend
│   ├── postgres_impl.py     # PostgreSQL storage backend
│   ├── milvus_impl.py       # Milvus vector storage backend
│   ├── networkx_impl.py     # NetworkX local graph storage
│   ├── faiss_impl.py        # Faiss local vector storage
│   ├── nano_vector_db_impl.py # NanoVectorDB local vector storage
│   ├── json_kv_impl.py      # JSON file KV storage
│   ├── json_doc_status_impl.py # JSON file DocStatus storage
│   ├── opensearch_impl.py   # OpenSearch storage backend
│   ├── memgraph_impl.py     # Memgraph graph storage backend
│   └── deprecated/          # Deprecated implementations
│
├── llm/                     # LLM call adaptation layer
│   ├── __init__.py
│   ├── openai.py            # OpenAI-compatible interface (primary)
│   ├── ollama.py            # Ollama local models
│   ├── azure_openai.py      # Azure OpenAI
│   ├── anthropic.py         # Anthropic Claude
│   ├── gemini.py            # Google Gemini
│   ├── bedrock.py           # AWS Bedrock
│   ├── hf.py                # HuggingFace
│   ├── jina.py              # Jina Embeddings
│   ├── zhipu.py             # Zhipu AI
│   ├── nvidia_openai.py     # NVIDIA OpenAI-compatible
│   ├── lmdeploy.py          # LMDeploy
│   ├── lollms.py            # LoLLMs
│   ├── llama_index_impl.py  # LlamaIndex bridge
│   └── binding_options.py   # Binding options
│
├── api/                     # REST API service layer
│   ├── lightrag_server.py   # FastAPI application entry
│   ├── config.py            # Server configuration
│   ├── auth.py              # Authentication
│   ├── utils_api.py         # API utility functions
│   ├── runtime_validation.py# Runtime validation
│   ├── run_with_gunicorn.py # Gunicorn production deployment
│   ├── gunicorn_config.py   # Gunicorn configuration
│   └── routers/             # API routes
│       ├── document_routes.py  # Document insert/delete/status
│       ├── graph_routes.py     # Graph CRUD
│       ├── query_routes.py     # Query endpoints
│       └── ollama_api.py       # Ollama-compatible API
│
├── tools/                   # Operations tools
│   ├── check_initialization.py     # Initialization check
│   ├── clean_llm_query_cache.py    # Clean LLM cache
│   ├── migrate_llm_cache.py        # Migrate LLM cache
│   ├── download_cache.py           # Download cache
│   ├── prepare_qdrant_legacy_data.py # Qdrant legacy data migration
│   └── lightrag_visualizer/        # Visualization tool
│
├── evaluation/              # RAG evaluation
│   ├── eval_rag_quality.py         # RAGAS evaluation script
│   └── sample_documents/           # Sample documents
│
├── tests/                   # Tests
│   ├── e2e/                        # End-to-end tests
│   │   ├── test_pipeline_e2e.py    # Main E2E test
│   │   ├── debug_energy_single.py  # Single economic text debug
│   │   ├── debug_bulk_six.py       # Bulk economic text debug
│   │   └── debug_schema_compare.py # General vs economy schema comparison
│   └── locks/                      # Lock mechanism tests
│
└── docs/
    └── CHANGES.md           # Complete change log
```

---

## 3. Architecture Constraints (Must Follow)

### 3.1 lightrag_fork Is a Pure Backend; Business Logic Is Forbidden

- **Do not** add Agent orchestration, conversation memory, user profiles, tool invocation, or other business code to this directory
- **Do not** introduce any dependency on `kg_agent/` (dependency direction is strictly `kg_agent/ → lightrag_fork/`)
- The upper business layer accesses this module via `from lightrag_fork import LightRAG, QueryParam`

### 3.2 Minimally Invasive Modification Principle

- This fork is based on upstream HKUDS/LightRAG; the modification strategy is **minimally invasive**
- Do not rewrite existing main flows (document chunking, entity extraction, graph merging, retrieval generation)
- New capabilities are implemented via **modular additions**, without modifying `prompt.py` static templates

### 3.3 Backward Compatibility

- All new features are disabled by default (`domain_schema.enabled=False`, lock backend defaults to `local`)
- When new configuration is not provided, behavior must be identical to the original LightRAG
- New environment variables have sensible defaults; missing values should not cause errors

---

## 4. Enhancements Over Upstream

### 4.1 Redis Distributed Locks

**Modified files:** `kg/lock_backend.py`, `kg/redis_lock_backend.py`, `kg/shared_storage.py`

- Abstracted unified lock interface `LockBackend`, hiding differences between local and Redis locks
- Redis locks use Lua scripts for atomic acquire / renew / release
- Supports auto-renewal (`auto_renew`), TTL management, graceful release
- Backend and failure strategy selectable via environment variables

**Lock hierarchy:**

```
Business code
  ↓
shared_storage.py helpers
  ├─ get_pipeline_runtime_lock()    # Pipeline execution mutual exclusion
  ├─ get_pipeline_enqueue_lock()    # Document enqueue mutual exclusion
  └─ get_storage_keyed_lock()       # Graph node/relation granular locks
  ↓
LockBackend (abstract)
  ├─ LocalLockBackend               # Single-machine local lock
  └─ RedisLockBackend               # Redis distributed lock
       └─ RedisLockManager          # Lua script + renewal task
```

**Related environment variables:**

| Variable | Default | Description |
|---|---|---|
| `LIGHTRAG_LOCK_BACKEND` | `local` | `local` / `redis` |
| `LIGHTRAG_LOCK_FAIL_MODE` | `strict` | `strict` / `fallback_local` |
| `LIGHTRAG_LOCK_KEY_PREFIX` | `lightrag:lock` | Redis key prefix |
| `LIGHTRAG_LOCK_RENEW_INTERVAL_S` | `None` | Renewal interval; defaults to ttl/3 if empty |
| `LIGHTRAG_PIPELINE_RUNTIME_LOCK_WAIT_TIMEOUT_S` | `0` | Runtime lock wait timeout |
| `LIGHTRAG_PIPELINE_ENQUEUE_LOCK_WAIT_TIMEOUT_S` | `None` | Enqueue lock wait timeout |

### 4.2 Modular Domain Schema

**Modified files:** `schema.py`, `schemas/`, `operate.py`, `lightrag.py`, `constants.py`

- Configured via `addon_params["domain_schema"]` entry point
- `schema.py` handles normalization: external input → `DomainSchema` dataclass → runtime dict
- `operate.py::extract_entities()` appends domain constraint block at the end of the prompt
- `schema.py` also provides post-parse canonicalization helpers so extracted entity types and relation keywords can be normalized back onto schema-defined canonical names when the schema is enabled
- Keep economy relation aliases narrow and phrase-level. Avoid generic single-verb aliases such as `支持`, `影响`, or `属于`, because `relationship_keywords` are soft semantic labels rather than strict graph predicates and broad aliases will collapse unrelated edges onto the wrong canonical relation type.
- `schema.py` now also carries an optional `explanation_profile` block on each built-in schema so upper layers can reuse domain-aware intent/tag/relation/evidence/output/guardrail contracts, node-role/path-constraint structure, plus optional `scenario_overrides` without hardcoding them inside `kg_agent`; the lower layer only exposes `prompt_bindings/template_id` hints, while actual prompt text remains in the upper-layer registry
- The built-in general/economy explanation profiles are now intentionally organized into editable section blocks in `schemas/general.py` and `schemas/economy.py` (`supported_intents`, `intent_bindings`, `semantic_tags`, `relation_semantics`, `node_role_rules`, `path_constraints`, `evidence_policies`, `output_contracts`, `scenario_overrides`, `prompt_bindings`) so domain customization can usually stay inside the profile file instead of modifying the upper-layer explainer
- Does not modify `prompt.py` static templates

**Schema parameter passing chain (explicit, no global variables):**

```
LightRAG(addon_params=...)
  → lightrag.py::__post_init__()
    → schema.py::normalize_addon_schema_config()
      → global_config["addon_params"]["domain_schema"]
        → operate.py::extract_entities()
          → _build_domain_schema_prompt_appendix()
```

**Built-in schema profiles:**

| Profile | File | Mode | Entity Types | Explanation Profile |
|---|---|---|---|---|
| `general` | `schemas/general.py` | `enabled=False` | Person, Organization, Location... (original defaults) | `general_explainer` |
| `economy` | `schemas/economy.py` | `enabled=True, mode=domain` | Company, Industry, Metric, Policy, Event, Asset, Institution, Location, Person, Concept | `economy_explainer` |

The economy profile keeps entity-type canonicalization schema-managed: aliases live on `EntityTypeDefinition` / `DomainSchema.aliases`, and strict prompt/fallback behavior is declared under `DomainSchema.metadata` instead of hardcoded in upper-layer business code. Its default natural-language extraction/summary output is Chinese, while canonical schema identifiers remain stable English names for storage and filtering.

### 4.3 Dynamic-Graph Temporal Metadata

**Modified files:** `operate.py`, `utils.py`

- Node and edge merge paths now preserve `created_at` for existing graph elements
- New graph elements initialize:
  - `created_at`
  - `last_confirmed_at`
  - `confirmation_count=1`
- Existing graph elements update:
  - keep original `created_at`
  - refresh `last_confirmed_at`
  - increment `confirmation_count`
- Query result formatting passes through:
  - entity / relationship `created_at`
  - entity / relationship `last_confirmed_at`
  - entity / relationship `confirmation_count`
  - entity / relationship `rank`
- Query-time freshness-aware ranking can now be enabled through:
  - `QueryParam.enable_freshness_decay`
  - `QueryParam.staleness_decay_days`
- When enabled, `operate.py` applies freshness-aware reordering inside the KG retrieval pipeline and surfaces `metadata.freshness_decay_applied`

**Semantics:**

- `confirmation_count` currently means cumulative confirmation events, not unique independent sources
- Missing legacy fields must remain backward compatible and never crash query formatting

### 4.4 Utility LLM For Summary Tasks

**Modified files:** `lightrag.py`, `operate.py`, `api/lightrag_server.py`

- Entity/relation description summarization in `operate.py::_summarize_descriptions()` now prefers an optional dedicated lightweight utility LLM (`utility_llm_model_func`)
- If the utility LLM is not configured, the summary path falls back to the main `llm_model_func` and emits a one-time warning per workspace/task
- Direct API-server bootstrapping can provide the utility summary model through optional environment variables:
  - `UTILITY_LLM_BINDING`
  - `UTILITY_LLM_MODEL`
  - `UTILITY_LLM_BINDING_HOST`
  - `UTILITY_LLM_BINDING_API_KEY`
  - `UTILITY_LLM_TIMEOUT`
- Agent-managed bootstrapping can also inject `utility_llm_model_func` when building `LightRAG`

---

## 5. Core Data Flows

### 5.1 Document Insertion Flow

```
User calls LightRAG.ainsert(documents)
  → lightrag.py: document deduplication, filter_keys, optional `metadatas` / `segment_docs`
  → operate.py: chunking_by_token_size()      # Text chunking, with optional segment/page metadata propagation
  → operate.py: extract_entities()             # LLM entity/relation extraction
      ├─ prompt.py: static prompt templates
      ├─ operate.py: _build_domain_schema_prompt_appendix()  # Optional schema append
      └─ schema.py: post-parse type/keyword canonicalization  # Optional schema normalization
  → operate.py: merge_nodes_and_edges()        # Graph merging (with keyed lock + temporal metadata updates)
  → kg/*_impl: write to Graph / Vector / KV / DocStatus
```

### 5.2 Query Flow

```
User calls LightRAG.aquery(query, param)
  → operate.py: kg_query() / naive_query()
      ├─ Keyword extraction (LLM)
      ├─ Vector retrieval (entities_vdb / relationships_vdb / chunks_vdb)
      ├─ Graph traversal (graph_storage)
      ├─ Text chunk recall
      ├─ Reference formatting uses chunk `file_path` plus optional page/section/source metadata
      ├─ raw result assembly keeps `rank`
      ├─ `utils.convert_to_user_format()` passes temporal metadata through to user-facing results
      └─ LLM generates final answer
```

### 5.3 Retrieval Modes

| Mode | Description |
|---|---|
| `naive` | Vector retrieval on text chunks only |
| `local` | Entity-centric subgraph + relation context |
| `global` | Relation-centric high-level semantic retrieval |
| `hybrid` | local + global merged |
| `mix` | hybrid + naive merged |

---

## 6. Storage Backend Configuration

Storage backends are managed through the `STORAGES` registry in `kg/__init__.py`. Four storage types are configured independently:

| Storage Type | Abstract Interface | Recommended (Production) | Recommended (Local Dev) |
|---|---|---|---|
| KV_STORAGE | `BaseKVStorage` | MongoKVStorage / RedisKVStorage | JsonKVStorage |
| VECTOR_STORAGE | `BaseVectorStorage` | QdrantVectorDBStorage / MilvusVectorDBStorage | NanoVectorDBStorage / FaissVectorDBStorage |
| GRAPH_STORAGE | `BaseGraphStorage` | Neo4JStorage | NetworkXStorage |
| DOC_STATUS_STORAGE | `DocStatusStorage` | MongoDocStatusStorage | JsonDocStatusStorage |

**Current production combination used in this project:** Neo4j + Qdrant + MongoDB (KV + DocStatus)

---

## 7. Development Commands

```bash
# Activate virtual environment
.venv\Scripts\Activate.ps1                     # Windows PowerShell
source .venv/bin/activate                       # Linux/Mac

# Run E2E tests (requires Neo4j / Qdrant / MongoDB / LLM services)
python -m lightrag_fork.tests.e2e.test_pipeline_e2e

# Run schema comparison test
python -m lightrag_fork.tests.e2e.debug_schema_compare

# Single economic text debug
python -m lightrag_fork.tests.e2e.debug_energy_single

# Start API service
python -m lightrag_fork.api.lightrag_server
```

**Environment variable requirements:** `.env` file in the project root or under `lightrag_fork/`, containing:

- LLM config: `LLM_MODEL`, `LLM_BINDING`, `LLM_BINDING_HOST`
- Optional utility-summary LLM config: `UTILITY_LLM_MODEL`, `UTILITY_LLM_BINDING_HOST` and optional `UTILITY_LLM_BINDING`, `UTILITY_LLM_BINDING_API_KEY`, `UTILITY_LLM_TIMEOUT`
- Storage config: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `QDRANT_URL`, `QDRANT_COLLECTION_PREFIX`, `MONGO_URI`, `MONGO_DATABASE`
- Lock config: `LIGHTRAG_LOCK_BACKEND`, `REDIS_URI` (when using Redis locks)

---

## 8. Guidelines for Modifying This Directory

### 8.1 Safety Checklist

Before modifying code under `lightrag_fork/`, verify the following:

- [ ] Does the modification maintain backward compatibility? (Behavior unchanged when new config is not provided)
- [ ] Does it affect the calling interface used by the upper layer `kg_agent/`?
- [ ] Does it modify `prompt.py` templates? (Should be avoided whenever possible)
- [ ] Do new environment variables have sensible defaults?
- [ ] Does it introduce a dependency on `kg_agent/`? (Absolutely forbidden)
- [ ] Does it break graph/vector storage structure? (Would cause existing data incompatibility)

### 8.2 Common Modification Patterns

| Scenario | Recommended Approach |
|---|---|
| Add storage backend | Create `xxx_impl.py` under `kg/`, register in `kg/__init__.py` |
| Add LLM adapter | Create new file under `llm/` |
| Add domain schema | Create profile file under `schemas/`, export in `schemas/__init__.py` |
| Modify extraction behavior | Prefer schema injection and schema-based post-parse normalization; avoid directly rewriting `operate.py` main flow |
| Change entity/relation summary model selection | Prefer wiring `utility_llm_model_func` / `utility_llm_model_name` over hardcoding another summary path |
| Modify query behavior | Handle by mode branch in `kg_query()` in `operate.py` |
| Extend dynamic-graph freshness ranking | Prefer changes in `operate.py` ranking paths over inventing fake scores in upper layers |
| Add API route | Create new route file under `api/routers/` |

### 8.3 Things Not to Do in This Directory

- Add Agent loops, ReAct reasoning, or tool calling logic
- Add conversation history management or user profile storage
- Add crawlers, news collection, or other business logic
- Import the `kg_agent` package or any of its submodules

---

## 9. Key Classes and Functions Quick Reference

| Class/Function | File | Purpose |
|---|---|---|
| `LightRAG` | `lightrag.py` | Main entry class: initialization, insertion, query |
| `QueryParam` | `base.py` | Query parameters (mode, top_k, retrieval options, etc.) |
| `extract_entities()` | `operate.py` | Core entity/relation extraction function |
| `merge_nodes_and_edges()` | `operate.py` | Graph node/relation merging (with locks) |
| `kg_query()` | `operate.py` | local/global/hybrid/mix retrieval |
| `naive_query()` | `operate.py` | Naive vector-only retrieval |
| `convert_to_user_format()` | `utils.py` | Convert internal query data to user-facing format and pass through temporal metadata |
| `DomainSchema` | `schema.py` | Domain schema data structure |
| `normalize_addon_schema_config()` | `schema.py` | Schema config normalization |
| `LockBackend` | `kg/lock_backend.py` | Lock interface abstraction |
| `RedisLockBackend` | `kg/redis_lock_backend.py` | Redis distributed lock implementation |
| `KeyedUnifiedLock` | `kg/shared_storage.py` | Entity/relation granular lock |
| `STORAGES` | `kg/__init__.py` | Storage implementation registry |

---

## 10. Known Limitations and TODOs

- **Schema now affects prompt guidance plus post-parse canonicalization of extracted entity types / relation keywords**, and schema profiles may opt into strict entity-type fallback through `metadata`, but schema is still not integrated into graph storage structure, retrieval ranking, or query understanding
- **Built-in explanation profiles are still v1 contracts**; only `general` and `economy` are defined today, and their relation/evidence/output/guardrail/scenario plus node-role/path-constraint semantics are consumed by the upper-layer explainer rather than enforced inside the lower-layer retrieval/storage pipeline
- **Relation type constraints are weak**; guided only via prompts, no post-processing normalization
- **Redis distributed locks** still have window risks during master-slave failover/network partition scenarios
- **`fallback_local`** is only suitable for development degradation; does not provide strong consistency
- **No global transactions across storage backends**; inserts/deletes use eventual consistency
- **Freshness-aware ranking** now exists in the core KG retrieval flow, but it is still a v1 heuristic layered on existing ranking signals rather than a full ranking-model redesign
- **`confirmation_count` semantics** are cumulative confirmation events; there is no built-in notion of unique-source confirmation count yet
- **Chunk provenance is best-effort across storage backends**; JSON/Mongo/Redis keep full chunk metadata, PostgreSQL stores chunk metadata as JSONB, and vector backends expose metadata according to their payload-field support

---

## 11. Change History

See [docs/CHANGES.md](docs/CHANGES.md) for the complete change log.
