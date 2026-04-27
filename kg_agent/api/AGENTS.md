# AGENTS.md - kg_agent/api

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/api/` exposes the HTTP-facing surface for the business-layer agent.

It is responsible for:

- FastAPI application assembly and lifecycle management
- Environment-driven bootstrap of `AgentCore`, `LightRAG`, crawler, scheduler, and MCP transport
- HTTP route definitions and request/response models
- Cross-origin browser access policy for local frontend testing
- Uniform HTTP error envelopes
- Health and readiness endpoints

It is not responsible for:

- Core agent planning logic
- Native tool implementations
- Crawl policy internals
- Skill runtime server implementation

---

## 2. Key Files And Responsibilities

```text
api/
|-- app.py             # FastAPI factory, lifecycle, config wiring, exception handlers, /health, /ready, and /webui mount
|-- agent_routes.py    # Core chat/ingest/tool/skill HTTP models and handlers
|-- webui_routes.py    # WebUI-facing uploads, sessions, workspaces, graph, and discover endpoints
`-- webui/             # Built frontend assets generated from ../EconRAGent_webui/
```

**Current HTTP surface includes:**

- `POST /agent/chat`
- `POST /agent/ingest`
- `POST /agent/uploads`
- session routes under `/agent/sessions`
- workspace/import routes under `/agent/workspaces` and `/agent/imports`
- graph routes under `/agent/graph/*`
  - `GET /agent/graph/schema` exposes the active `lightrag_fork/schemas` entity and relation templates for WebUI filters, optionally scoped by `workspace`.
- discover routes under `/agent/discover/*`
- `GET /agent/tools`
- `GET /agent/skills`
- `GET /agent/skills/{skill_name}`
- `GET /agent/skills/{skill_name}/files/{relative_path}`
- `POST /agent/skills/{skill_name}/invoke`
- `GET/POST /agent/skill-runs/...`
- `POST /agent/capabilities/{capability_name}/invoke`
- `POST /agent/route_preview`
- scheduler and monitored-source routes
- `/webui` static SPA mount plus `/webui/chat`, `/webui/graph`, `/webui/discover`, `/webui/spaces`
- `GET /health`
- `GET /ready`

---

## 3. HTTP Contract Rules

- Browser-facing development support goes through FastAPI CORS middleware configured by `KG_AGENT_CORS_ORIGINS`.
- `POST /agent/chat` remains the primary chat endpoint for both normal and streaming flows.
- Streaming mode is still activated by `stream=true` in the request body and returns `text/event-stream`.
- The frontend source of truth is `EconRAGent_webui/`; files under `kg_agent/api/webui/` are generated assets that should be refreshed by the frontend build rather than hand-edited.
- Upload-backed workspace imports depend on `kg_agent.uploads.UploadStore` text and manifest extraction. Keep API behavior aligned with the WebUI import dialog for Word `.docx`, PDF, Markdown `.md`/`.markdown`, and EPUB.
- `POST /agent/uploads` is shared by workspace import and chat attachments. It may still accept text files and images for other flows, but legacy Word `.doc` should be rejected with a clear 400 because the current extraction/chat path only supports `.docx`.
- Workspace import routes should keep `file_path` stable for citations while passing document provenance through `metadatas` and structured segments through `segment_docs`; URL imports sourced by Crawl4AI should use `source_label="crawler"`.
- `build_rag_from_env()` and `EnvLightRAGProvider` should pass `KG_AGENT_DEFAULT_DOMAIN_SCHEMA` / `config.runtime.default_domain_schema` into LightRAG `addon_params`. The project default is `economy`; setting it to `general` is the explicit opt-out path.
- Agent-managed LightRAG construction defaults extraction/summary language to Chinese through `SUMMARY_LANGUAGE` / `KG_AGENT_SUMMARY_LANGUAGE`; schema canonical names such as `Company` or `policy_supports` remain internal identifiers while WebUI labels should use `display_name`.
- Graph routes treat `workspace=all` as a Web/API aggregation sentinel. Do not pass `all` into `LightRAG` as if it were a real workspace; resolve registered workspaces, query them individually, and merge the public payload at the API layer.
- Workspace deletion/drop paths should clear `llm_response_cache` together with graph/vector/KV/doc-status storage so stale extraction outputs do not survive a delete-and-reimport test cycle.
- Graph route node budgets are intentionally aligned across layers: the current WebUI default request budget is `400`, and `/agent/graph/overview` plus `/agent/graph/subgraph` currently enforce `max_nodes <= 400`.
- Graph filter option labels should stay schema-driven. The WebUI reads `/agent/graph/schema`; do not duplicate entity or relation type lists in frontend source when they already exist under `lightrag_fork/schemas`. If no workspace runtime schema is available, graph filters should fall back to the economy schema because the general schema intentionally has no relation type definitions.
- Graph filtering should canonicalize entity and relation filter values through the active schema aliases/display names before comparing them with graph node/edge properties. The filter sidebar itself should stay schema-only: `/agent/graph/schema` should expose the configured `RelationTypeDefinition` / `EntityTypeDefinition` options, while legacy free-form edge keywords may still be matched if a client manually sends them as filter values.
- For `workspace=all` graph requests, budget allocation is two-stage: first distribute node budget across workspaces, then recycle unused capacity to still-truncated workspaces so the merged response wastes less of the allowed node count.
- Graph entity and relation detail routes may enrich graph `source_id` chunk hashes with `source_metadata` resolved from `rag.text_chunks`; the frontend should display this readable provenance instead of raw chunk ids.
- SSE output is standardized as:
  - `event: <type>`
  - `data: <json>`
- Stream payloads still keep the existing `type` field in the JSON body for backward compatibility.
- HTTP errors must use the uniform envelope:
  - `error.code`
  - `error.message`
  - `error.status_code`
  - optional `error.details` for validation failures
- `/health` is an info/liveness-style endpoint and should remain broadly compatible.
- `/ready` is the readiness signal intended for frontend boot checks.

---

## 4. Modification Guidance

- Keep transport concerns in this directory. Do not move HTTP-specific error handling or SSE formatting into `agent_core.py`.
- Preserve compatibility for `POST /agent/chat + stream=true`; do not replace it casually with a new primary streaming route.
- Keep the browser-facing surface unified under `kg_agent`; the WebUI should not need to call `lightrag_fork` directly.
- If you change graph route defaults or limits, update all three layers together: `kg_agent/api/webui_routes.py`, `EconRAGent_webui/src/pages/GraphPage.tsx`, and the underlying `LightRAG` max-graph-node default wired in `app.py`.
- Use narrow route-level exception mapping for expected business cases such as `ValueError`, `LookupError`, and `RuntimeError`; let the app-level exception handlers normalize everything else.
- If you extend public response shapes, keep them explicit in the Pydantic models in `agent_routes.py`.
- Keep skill-document access progressive:
  - `GET /agent/skills/{skill_name}` returns skill metadata plus full `SKILL.md`
  - related files such as `references/*` or `scripts/*` should be fetched later through `GET /agent/skills/{skill_name}/files/{relative_path}` only when the document content makes them relevant
- Keep CORS and readiness behavior minimal and frontend-oriented unless there is a concrete product need for stronger deployment policy.

---

## 5. Known Limitations And TODOs

- Streaming is still fetch-oriented for browsers because the primary streaming contract remains `POST /agent/chat`; there is no dedicated GET/EventSource endpoint yet.
- The API is intentionally unauthenticated in this local-test-focused stage; any move toward shared or public deployment should revisit auth before broad exposure.
- `/ready` is a pragmatic frontend readiness signal, not a deep dependency probe of every external service.
- Frontend packaging currently depends on checked-in generated assets under `kg_agent/api/webui/`; if frontend source changes without rebuilding, packaged backend output will drift.
