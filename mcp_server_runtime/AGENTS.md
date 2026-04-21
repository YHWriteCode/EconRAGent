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
- explicit shell commands and CLI-arg entrypoints that target the runtime workspace root must be rewritten into the concrete run workspace before execution, except for the configured shared output root when `MCP_OUTPUT_DIR` is enabled
- Docker-facing `/workspace/...` run paths should remain mappable back to host-visible bind-mount paths when the runtime is mounted from the host
- free-shell bootstrap/setup should remain isolated from the image-global environment; use the runtime-provided bootstrap env vars rather than mutating global Python/npm state
- runtime workspaces should separate minimal execution input from skill-document context; keep `skill_invocation.json` and `skill_context.json` distinct even if `SKILL_REQUEST_FILE` remains as a compatibility alias to the invocation file

---

## 5. Testing And Refactor Notes

- `tests/kg_agent/test_skill_runtime_service.py` loads `mcp-server/server.py` by path and accesses module-level symbols directly.
- Refactors inside this package must assume `server.py` will continue re-exporting selected helpers for compatibility.
- If a change touches import-time configuration, verify repeated module loads with different env vars.
- If a change touches queue/execution timing, verify both:
  - immediate background return behavior
  - later polling via `get_run_status`, `get_run_logs`, and `get_run_artifacts`
  - durable-worker repair still runs when the current in-process utility LLM client is stale or unavailable, and the worker refreshes it before deciding whether repair is possible
  - durable-worker state transitions are observable: a claimed run should advance into `worker_starting` before env/bootstrap work and then into `executing` once the main shell command begins
- If a change touches workspace-path handling, verify both:
  - outputs requested under runtime-root absolute paths end up inside the active run workspace
  - host-side artifact fallback still works after any workspace-path remapping
  - shared output paths under `/workspace/output/...` still land in the configured shared output root and are visible in run artifacts
- If a change touches generated-script execution, verify both:
  - `.py` entrypoints still materialize through Python
  - `.js`, `.mjs`, and `.cjs` entrypoints materialize through `node <script>` rather than relying on executable permission bits in the workspace
- If a change touches bootstrap or dependency setup, verify both:
  - free-shell bootstrap lands in the shared env/bootstrap area rather than the image-global environment
  - runtime env vars such as `SKILL_BOOTSTRAP_*`, `PIP_TARGET`, and `NPM_CONFIG_PREFIX` still point at the intended isolated bootstrap root
- If a change adds new package files, keep Docker packaging and `pyproject.toml` package discovery updated.

---

## 6. Current Implementation Notes

- `server.py` now instantiates `RuntimeRunStore`, `QueueRuntimeManager`, `RuntimeExecutionManager`, `RuntimeService`, transport bindings, and CLI dependencies, then re-exports compatibility wrappers.
- `execution.py` uses injected callbacks so it can stay decoupled from the file-entrypoint facade.
- `queue.py` owns the in-process worker table, but `server.py` re-exports that state through `QUEUE_WORKER_PROCESSES` for tests and compatibility.
- `store.py` owns the SQLite schema and in-memory mirror, but `server.py` re-exports `RUN_STORE` and helper functions for compatibility.
- `envs.py` now supports two isolated dependency surfaces:
  - declared per-skill shared venvs keyed by dependency files such as `runtime_requirements`, `requirements.lock`, or `requirements.txt`
  - shared free-shell bootstrap roots under `ENVS_ROOT/bootstrap/...` exposed through `SKILL_BOOTSTRAP_*`, `PIP_TARGET`, and `NPM_CONFIG_PREFIX`
- `planning.py` and repair planning consume the full `SKILL.md`, not only a truncated excerpt. Natural-language dependency/setup guidance anywhere in the document can drive free-shell bootstrap commands.
- `planning.py` preflight now rejects generated Python helpers that import known Node-only packages such as `pptxgenjs`, `react`, or `sharp`; those plans must be repaired into a native Node entrypoint or another valid shape before execution.
- `workspace.py` stages the execution loop as two files rather than one mixed blob:
  - `skill_invocation.json` for the durable request/execution payload
  - `skill_context.json` for the full `SKILL.md`
- `workspace.py` now also differentiates between a full internal run record and a compact MCP transport view:
  - internal/store payloads may retain richer planner/runtime diagnostics
  - transport payloads should expose only execution-relevant fields plus compact summaries such as inferred constraints, blocker classification, compact planner warnings, and bounded repair/bootstrap history
  - do not reintroduce `file_inventory`, `references`, `shell_hints`, full doc bundles, or large script/path dumps into public run-task/status payloads
- `envs.py` and `planning.py` cooperatively rewrite runtime-root absolute command paths. General `/workspace/...` paths become run-local paths, while `/workspace/output/...` can be redirected into `MCP_OUTPUT_DIR/<run-id>/...` so user-visible artifacts persist in a shared host directory such as the repository-root `skill_output/`.
- `envs.py` now treats Node-flavored generated entrypoints such as `.js`, `.mjs`, and `.cjs` as explicit Node scripts, materializing them as `node <script>` inside both skill-root and workspace-root command builders instead of invoking the file directly.
- `workspace.py` mirrors run-local `output/` files into the configured shared output root and deduplicates public `output/...` artifact paths so callers see a stable artifact list.
- `execution.py` normalizes bootstrap `pip install` commands with upgrade flags so repeated writes into the shared bootstrap target do not get stuck on existing-package directory collisions.
- `execution.py` now refreshes the utility LLM client before bounded repair decisions inside durable workers. This closes the case where a worker reports `process_failed` with `repair_attempted=false` only because its inherited in-process client became stale or unavailable.
- `execution.py` now marks a claimed durable run as `worker_starting` as soon as the worker actually enters `run_durable_worker()`, so queue-state polling can distinguish "claimed but not yet entered" from "already inside worker setup".
