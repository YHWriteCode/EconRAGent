# AGENTS.md - kg_agent

> This file is for AI coding assistants working in `kg_agent/`.
> Use it as the entry guide, then open the subsystem-specific `AGENTS.md` files for deeper detail.

---

## 1. Module Positioning

`kg_agent/` is the business-layer agent system built on top of `lightrag_fork/`.

**Core responsibilities:**

- Agent orchestration and final response generation
- Planner-visible capability routing and tool execution
- Local skill catalog, planning, and runtime handoff
- Path explanation over graph paths and text evidence
- Conversation memory, cross-session memory, and user profile handling
- Web crawling, recurring ingest, and source management
- FastAPI service and dynamic workspace bootstrapping

**Not responsible for:**

- Document chunking, entity extraction, graph merging, and storage backends
- Low-level graph/vector/KV/doc-status implementations
- Upstream RAG model bindings owned by `lightrag_fork/`

**Dependency direction:**

```text
kg_agent/ -> lightrag_fork/
lightrag_fork/ -X-> kg_agent/
```

Deeper subsystem details have been moved into child `AGENTS.md` files to keep this root guide short.

---

## 2. Main Directory Structure

```text
kg_agent/
|-- __init__.py
|-- config.py
|-- AGENTS.md
|-- agent/      # Agent loop, routing, prompts, capability and tool registries
|-- api/        # FastAPI app factory, routes, HTTP contract, CORS and health/readiness
|-- crawler/    # Crawl4AI adapter, source registry, scheduler, crawl state
|-- mcp/        # MCP transport adapter for external capabilities
|-- memory/     # Session memory, cross-session memory, user profiles
|-- skills/     # Skill catalog, loader, planner, executor, runtime client
`-- tools/      # Native built-in tools and tool result definitions
```

---

## 3. Subsystem Docs

- [agent/AGENTS.md](agent/AGENTS.md) - Agent loop, Route Judge, Path Explainer, prompts, and capability/tool registration.
- [api/AGENTS.md](api/AGENTS.md) - FastAPI bootstrap, route contracts, uniform error envelopes, CORS, and health/readiness behavior.
- [crawler/AGENTS.md](crawler/AGENTS.md) - Crawling, feed/source policy, crawl-state persistence, and recurring ingest scheduling.
- [memory/AGENTS.md](memory/AGENTS.md) - Conversation memory, cross-session retrieval, and user profile storage.
- [skills/AGENTS.md](skills/AGENTS.md) - Skill catalog loading, command planning, runtime boundaries, and run-status semantics.
- [tools/AGENTS.md](tools/AGENTS.md) - Native tool contract, built-in tools, rerank helpers, and tool-layer constraints.

**Related runtime doc:**

- [../mcp-server/AGENTS.md](../mcp-server/AGENTS.md) - stdio MCP runtime server backing durable skill execution.

---

## 4. Cross-Cutting Rules

- Keep business logic in `kg_agent/`; do not move agent behavior into `lightrag_fork/`.
- Keep the dependency direction one-way; `lightrag_fork/` must never import `kg_agent/`.
- Do not introduce LangChain, LlamaIndex, or other heavy orchestration frameworks as core dependencies.
- Keep planner-visible native capabilities, local skills, and external MCP capabilities as separate surfaces.
- Keep skill catalog metadata explicit; planner matching depends on skill names, descriptions, and tags, so domain skills should publish strong bilingual keywords when needed.
- Keep shipped skill script contracts inferable when possible; relative-date phrases and domain identifiers should resolve into explicit planner constraints without weakening execution safety.
- Do not hardcode domain-specific placeholder tools into the native built-in registry; specialized integrations belong behind MCP or the skill runtime.
- Prefer `rag_provider`-based dynamic workspace loading in API-facing flows instead of binding the app to one fixed `LightRAG` instance.

---

## 5. Project-Level Limitations And TODOs

- MCP integration is still stdio-first. The stack does not yet provide richer SSE or HTTP transport negotiation across the agent and runtime layers.
- Configured `external_mcp` capabilities execute correctly when called directly, but auto-routing still depends on explicit `planner_exposed=true`.
- Dynamic-graph freshness and lifecycle behavior is distributed across retrieval, crawler, and lower-layer RAG support; some compatibility fallbacks still exist for older metadata shapes.
- Path explanation already consumes schema-provided explanation profiles, but only the built-in `general` and `economy` profiles exist today.
- Detailed subsystem-specific constraints and TODOs now live in the child docs listed above.
