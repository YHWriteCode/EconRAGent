# AGENTS.md - repository root

This file is the top-level guide for AI coding assistants and contributors working in this repository.

---

## 1. First Rules

- If you are running in a sandbox and need to use the project's `.venv`, ask the user for permission before invoking it.
- Keep the dependency direction one-way: `kg_agent/ -> lightrag_fork/`.
- Do not put `node_modules/` into git. Frontend reproducibility comes from `package.json` plus `EconRAGent_webui/package-lock.json`.

---

## 2. Module Map

```text
EconRAGent repo root
|-- AGENTS.md             # This file
|-- README.md             # Human-facing project overview and setup
|-- scheduler_sources.json # Local JSON scheduler source registry
|-- lightrag_fork/        # Graph/vector backend layer
|-- kg_agent/             # Agent orchestration and unified API layer
|-- EconRAGent_webui/     # React + TypeScript + Vite frontend source
`-- mcp-server/           # MCP skill runtime entrypoint
```

Open the deeper module guides when you work in those areas:

- [kg_agent/AGENTS.md](kg_agent/AGENTS.md)
- [kg_agent/api/AGENTS.md](kg_agent/api/AGENTS.md)
- [kg_agent/agent/AGENTS.md](kg_agent/agent/AGENTS.md)
- [lightrag_fork/AGENTS.md](lightrag_fork/AGENTS.md)
- [mcp-server/AGENTS.md](mcp-server/AGENTS.md)
- [EconRAGent_webui/AGENTS.md](EconRAGent_webui/AGENTS.md)

---

## 3. Frontend Workflow

- Frontend source lives in `EconRAGent_webui/`.
- Built static assets are emitted into `kg_agent/api/webui/` and are served by `kg_agent`.
- `EconRAGent_webui/node_modules/` is local-only and must not be committed.
- `EconRAGent_webui/package-lock.json` should be committed so other developers install the exact tested dependency graph.
- When frontend source changes, update the built assets in `kg_agent/api/webui/` before shipping a packaged backend or asking others to verify the same UI.

Recommended commands:

```bash
cd EconRAGent_webui
npm install
node .\node_modules\vitest\vitest.mjs run
node .\node_modules\vite\bin\vite.js build
```

---

## 4. Documentation Hygiene

- Keep `README.md` aligned with the current developer workflow.
- Keep `.env.example` aligned with runtime workspace policy, including `KG_AGENT_DEFAULT_WORKSPACE` and `KG_AGENT_NETWORK_INGEST_WORKSPACE`.
- Keep `scheduler_sources.json` as normal `MonitoredSource` JSON payloads. Recurring network ingest sources should target the dedicated network-ingest workspace unless there is a specific reason to isolate them elsewhere.
- If you add or move public API routes, update `kg_agent/api/AGENTS.md` and the README API/setup sections.
- If you add a new top-level subsystem, give it its own `AGENTS.md` instead of overloading unrelated guides.
