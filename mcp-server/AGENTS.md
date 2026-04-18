# AGENTS.md - mcp-server

> Related root guide: [../kg_agent/AGENTS.md](../kg_agent/AGENTS.md)

---

## 1. Module Positioning

`mcp-server/` is the stdio MCP runtime entrypoint that backs durable shell-oriented skill execution.

After the runtime split, most implementation now lives in `../mcp_server_runtime/`.

`mcp-server/server.py` is intentionally kept as a stable file entrypoint and compatibility facade. It still owns:

- direct script startup via `python mcp-server/server.py`
- queue-worker and single-run worker CLI modes
- module-level compatibility exports used by tests and older callers
- object wiring for the extracted runtime modules

It is responsible for:

- Exposing skill catalog and run-lifecycle tools over FastMCP
- Materializing runtime workspaces for skill runs
- Persisting run records in a SQLite-backed durable store
- Running queue workers and durable worker processes
- Serving logs, artifacts, and compatibility skill resources
- Packaging the runtime for containerized stdio execution

It is not responsible for:

- Agent routing decisions
- Native built-in tool execution
- Conversation memory
- Lower-layer graph storage

---

## 2. Key Files And Responsibilities

```text
mcp-server/
|-- server.py             # Stable script entrypoint and compatibility facade
|-- Dockerfile            # Container image for stdio MCP runtime
|-- docker-compose.yml    # Local container launch wrapper for stdio runtime
`-- requirements.txt      # Runtime dependencies bundled into the server image

../mcp_server_runtime/
|-- config.py             # Environment-derived paths, limits, and utility LLM setup
|-- skills.py             # Skill discovery, payload loading, file classification
|-- envs.py               # Per-skill environment hashing and materialization
|-- workspace.py          # Workspace mirroring, requests, snapshots, artifacts
|-- planning.py           # Command-plan resolution, preflight, bounded repair hooks
|-- store.py              # SQLite durable run store
|-- queue.py              # Queue lease, worker spawn, stale-run recovery
|-- execution.py          # Bootstrap, shell execution, durable worker loop
|-- service.py            # High-level runtime operations
|-- transport.py          # FastMCP tool/resource bindings
`-- cli.py                # CLI argument handling and startup dispatch
```

**What `server.py` still owns:**

- stable script path and startup semantics
- compatibility wrappers around extracted runtime managers
- top-level runtime wiring and dependency injection
- module reload behavior for test isolation when the file is loaded repeatedly by path

**What moved into `mcp_server_runtime/`:**

- store, queue, and shell execution internals
- service-layer runtime APIs
- MCP transport registration
- CLI dispatch logic
- most helper and planning code

---

## 3. Runtime Surface

**Primary tools:**

- `list_skills`
- `read_skill`
- `read_skill_file`
- `read_skill_docs` (compatibility wrapper)
- `run_skill_task`
- `get_run_status`
- `cancel_skill_run`
- `get_run_logs`
- `get_run_artifacts`
- `execute_skill_script` (legacy compatibility wrapper)

**Primary resources:**

- `skill://catalog`
- `skill://{skill_name}`
- `skill://{skill_name}/docs`
- `skill://{skill_name}/files/{relative_path}`
- `skill://{skill_name}/references/{reference_name}`
- `skill-run://{run_id}/logs`
- `skill-run://{run_id}/artifacts`

**Runtime modes:**

- normal stdio MCP server mode
- queue-worker process mode
- single durable-worker mode for one claimed run

Environment defaults point the runtime at `/app/skills` for read-only skill content and `/workspace` for mutable run workspaces.

---

## 4. Modification Guidance

- Keep this service stdio-safe. It is launched as an MCP subprocess or container command, not as a long-lived HTTP service.
- Keep `mcp-server/server.py` present at the same path. Docker, compose, scripts, and tests depend on that file-level entrypoint.
- Keep `server.py` thin. New implementation code should go into `mcp_server_runtime/` unless it is specifically about entrypoint compatibility.
- Preserve the durable-run boundary: queue state, run records, logs, artifacts, and cancellation must remain recoverable from the SQLite store.
- Keep workspace writes under `MCP_WORKSPACE_DIR`; skill source content under `MCP_SKILLS_DIR` should stay read-only.
- Keep shell execution bounded by explicit timeout, bootstrap, and repair limits. Do not turn this server into an unbounded autonomous shell agent.
- Maintain compatibility wrappers such as `read_skill_docs` and `execute_skill_script` only as thin shims around the newer coarse-grained runtime APIs.
- Preserve CLI compatibility for `--queue-worker`, `--worker-run-id`, `--prefetch-skill-wheels`, and `--prefetch-all-skill-wheels`.
- Preserve MCP tool names, resource URIs, and response payload shapes. `kg_agent` clients normalize these fields directly.
- Preserve module-level exports from `server.py` that tests access directly, including run-store helpers, queue helpers, and execution helpers.
- Tests load `server.py` via `importlib.util.spec_from_file_location(...)`. Be careful with import-time config caching; repeated file-path loads must still pick up fresh environment variables.
- If you extend the container image, remember that the runtime depends on `kg_agent/` planner/runtime code as well as `skills/`.
- If you add new runtime modules under `mcp_server_runtime/`, keep Docker packaging and `pyproject.toml` package discovery in sync.
- If shipped skill files change, rebuild the runtime image even when `requirements.txt` is unchanged because `/app/skills` is baked into the container at build time.

---

## 5. Known Limitations And TODOs

- The durable run store is SQLite-backed and local-file only. There is still no shared Redis or Postgres queue/store for multi-node runtime coordination.
- Queue workers are still local host processes. There is no cross-node worker pool or richer distributed lease manager.
- `worker_lost` recovery stops at bounded requeue through `max_attempts`; there is no dead-letter queue or richer backoff strategy yet.
- Live progress is exposed through polling (`get_run_status`, `get_run_logs`, `get_run_artifacts`), not SSE or streaming job control.
- Artifact fallback only works well when Docker uses a host-visible bind mount for `/workspace`; named volumes or remote workers are not recoverable from the host side.
- The supported deployment pattern is still stdio MCP over `docker run` or `docker compose run`, not a long-lived HTTP or SSE runtime service.
- Legacy compatibility wrappers still exist for older callers, but they are no longer the primary runtime surface.
