# AGENTS.md - mcp_server_runtime

> Related entrypoint guide: [../mcp-server/AGENTS.md](../mcp-server/AGENTS.md)

---

## 1. Package Positioning

`mcp_server_runtime/` is the internal implementation package behind `mcp-server/server.py`.

This package is where new runtime code should usually live. `mcp-server/server.py` should remain a thin facade that:

- preserves the stable script entrypoint
- preserves module-level compatibility exports
- wires dependencies into the extracted runtime modules

This package is not the externally stable contract by itself. External callers still depend primarily on:

- the file path `mcp-server/server.py`
- the CLI flags handled by that file
- the MCP tool names and resource URIs exposed by the runtime
- the response payload fields returned by those tools

---

## 2. Module Responsibilities

```text
mcp_server_runtime/
|-- config.py      # Environment-derived paths, timeouts, constants, utility LLM setup
|-- errors.py      # Shared runtime exception types
|-- utils.py       # Small shell/time/text helpers
|-- skills.py      # Skill discovery, SKILL.md parsing, payload construction
|-- envs.py        # Skill environment hashing, metadata, wheelhouse/venv handling, runtime-root path rewriting
|-- workspace.py   # Workspace mirroring, request files, snapshots, artifacts
|-- planning.py    # Command planning, runtime target shaping, runtime-workspace materialization, preflight, repair planning
|-- store.py       # SQLite-backed durable run store
|-- queue.py       # Queue lease handling, worker spawn/reap, stale-run recovery
|-- execution.py   # Bootstrap, shell execution, durable worker orchestration
|-- service.py     # High-level runtime service methods
|-- transport.py   # FastMCP tool/resource bindings
`-- cli.py         # CLI entry handling and startup dispatch
```

Recommended layering:

- `config.py`, `errors.py`, `utils.py` are low-level foundations.
- `skills.py`, `envs.py`, `workspace.py`, `planning.py` provide reusable runtime mechanics.
- `store.py` owns durable persistence.
- `queue.py` owns queue/lease/process orchestration.
- `execution.py` owns runtime execution flows and worker loops.
- `service.py` exposes coarse-grained runtime operations.
- `transport.py` adapts service methods into MCP tools/resources.
- `cli.py` handles startup mode dispatch only.

---

## 3. Dependency Rules

Keep dependency flow one-way wherever practical:

```text
transport -> service
cli -> injected callables only
service -> planning/skills/envs/workspace/execution/store via injected dependencies
execution -> envs/planning/workspace/store/queue-compatible callbacks
queue -> store/workspace-compatible callbacks
store -> config/utils only
```

Rules to preserve:

- Do not make package modules import `mcp-server/server.py`.
- Prefer dependency injection over hard imports when crossing high-level boundaries.
- `transport.py` should not contain runtime business logic.
- `cli.py` should not contain runtime business logic.
- `store.py` should stay focused on durable persistence, not shell execution.
- `queue.py` should stay focused on claims, leases, worker lifecycle, and recovery.
- `execution.py` should stay focused on execution state transitions and bounded shell flows.

---

## 4. Compatibility Constraints

Changes here must not silently break the public runtime surface preserved by `mcp-server/server.py`.

Keep these stable unless the whole runtime contract is intentionally revised:

- tool names:
  - `list_skills`
  - `read_skill`
  - `read_skill_file`
  - `read_skill_docs`
  - `run_skill_task`
  - `get_run_status`
  - `cancel_skill_run`
  - `get_run_logs`
  - `get_run_artifacts`
  - `execute_skill_script`
- resource URIs:
  - `skill://catalog`
  - `skill://{skill_name}`
  - `skill://{skill_name}/docs`
  - `skill://{skill_name}/files/{relative_path}`
  - `skill://{skill_name}/references/{reference_name}`
  - `skill-run://{run_id}/logs`
  - `skill-run://{run_id}/artifacts`
- CLI flags:
  - `--queue-worker`
  - `--worker-run-id`
  - `--prefetch-skill-wheels`
  - `--prefetch-all-skill-wheels`
- payload fields expected by callers:
  - `run_status`
  - `status`
  - `runtime`
  - `runtime_target`
  - `command_plan`
  - `preflight`
  - `workspace`
  - `artifacts`
  - `cancel_requested`

Also preserve these behavioral constraints:

- repeated file-path loads of `mcp-server/server.py` must still pick up fresh env vars
- durable runs must remain recoverable from SQLite + workspace snapshots
- queue workers must still be startable by relaunching `mcp-server/server.py`
- shell execution must remain bounded by timeout, bootstrap, and repair limits
- explicit shell commands and CLI-arg entrypoints that target the runtime workspace root must be rewritten into the concrete run workspace before execution
- Docker-facing `/workspace/...` run paths should remain mappable back to host-visible bind-mount paths when the runtime is mounted from the host

---

## 5. Testing And Refactor Notes

- `tests/kg_agent/test_skill_runtime_service.py` loads `mcp-server/server.py` by path and accesses module-level symbols directly.
- Refactors inside this package must assume `server.py` will continue re-exporting selected helpers for compatibility.
- If a change touches import-time configuration, verify repeated module loads with different env vars.
- If a change touches queue/execution timing, verify both:
  - immediate background return behavior
  - later polling via `get_run_status`, `get_run_logs`, and `get_run_artifacts`
- If a change touches workspace-path handling, verify both:
  - outputs requested under runtime-root absolute paths end up inside the active run workspace
  - host-side artifact fallback still works after any workspace-path remapping
- If a change adds new package files, keep Docker packaging and `pyproject.toml` package discovery updated.

---

## 6. Current Implementation Notes

- `server.py` now instantiates `RuntimeRunStore`, `QueueRuntimeManager`, `RuntimeExecutionManager`, `RuntimeService`, transport bindings, and CLI dependencies, then re-exports compatibility wrappers.
- `execution.py` uses injected callbacks so it can stay decoupled from the file-entrypoint facade.
- `queue.py` owns the in-process worker table, but `server.py` re-exports that state through `QUEUE_WORKER_PROCESSES` for tests and compatibility.
- `store.py` owns the SQLite schema and in-memory mirror, but `server.py` re-exports `RUN_STORE` and helper functions for compatibility.
- `envs.py` and `planning.py` cooperatively rewrite runtime-root absolute command paths such as `/workspace/output/...` into the concrete per-run workspace so artifacts land in durable host-visible run directories instead of only inside the container view.
