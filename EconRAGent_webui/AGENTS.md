# AGENTS.md - EconRAGent_webui

> Related root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`EconRAGent_webui/` is the frontend source workspace for the unified KG Agent WebUI.

It is responsible for:

- React single-page application code
- Route-level UI for chat, graph, discover, and spaces
- Browser-side state with Zustand and TanStack Query
- Streaming chat rendering over the `kg_agent` HTTP API
- Building static assets for backend packaging

It is not responsible for:

- Direct access to `lightrag_fork/` APIs
- Persisting backend business state outside the browser
- Owning final package distribution; the shipped static assets live under `kg_agent/api/webui/`

---

## 2. Source Of Truth

- Editable source files live in `EconRAGent_webui/src/`.
- Built output goes to `../kg_agent/api/webui/` through Vite config.
- `node_modules/` is disposable local state and must never be committed.
- `package-lock.json` is the reproducibility anchor and should be committed whenever dependencies change.

---

## 3. Runtime And Data Rules

- Frontend calls only the unified `kg_agent` same-origin API surface.
- Do not wire the browser directly to `lightrag_fork`.
- Keep the current stack consistent unless there is a strong reason to change it:
  - React Router for route switching
  - TanStack Query for server-state fetching
  - Zustand for client state
  - Cytoscape.js for graph rendering

---

## 4. Validation Commands

Use the tested npm-based workflow:

```bash
cd EconRAGent_webui
npm install
node .\node_modules\vitest\vitest.mjs run
node .\node_modules\vite\bin\vite.js build
```

If frontend source changes, make sure the build step refreshes `kg_agent/api/webui/`.

---

## 5. Editing Guidance

- Keep the application Chinese-first unless a page already mixes languages intentionally.
- Preserve same-origin deployment assumptions; avoid adding environment-specific frontend API host logic unless required.
- Prefer updating tests when UI contracts, route structure, or request parameters change.
- Treat `kg_agent/api/webui/` as generated output. Edit source in `src/`, not the built files.

---

## 6. Current WebUI Layout Conventions

- `AppShell` owns the shared left sidebar and top navigation for `chat`, `graph`, and `discover`.
- The `spaces` route is a standalone management page: keep the left sidebar, hide the top navigation, and provide its own close/create controls.
- Avoid using visual-only movement such as `transform` for major layout placement when it affects scroll boundaries; prefer real grid/flex layout space.
- Keep `chat` and `graph` as fixed-viewport work surfaces. Only nested regions should scroll: chat message feed, graph filter panel, sidebar history, and spaces database list.
- `discover` is a news/feed page and may use normal page scrolling.
- Preserve the chat composer contract: attachment/search menu from the `+` button, retrieval mode buttons below the input, and auto-growing textarea with an internal max-height.
- Preserve the graph page contract: Cytoscape canvas as the primary surface, with a collapsible database filter sidebar that allows the canvas to expand.
