# AGENTS.md - kg_agent/skills

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/skills/` owns the local skill surface that sits beside native capabilities.

It is responsible for:

- Scanning and cataloging local `skills/*/SKILL.md`
- Loading skill docs and file inventory on demand
- Turning a skill request into a canonical shell-oriented command plan
- Executing that plan through a runtime client boundary
- Defining the structured data models used across planning and execution

It is not responsible for:

- Acting as a planner-visible MCP tool registry
- Implementing the durable runtime server itself
- Native built-in tool behavior

---

## 2. Key Files And Responsibilities

```text
skills/
|-- registry.py          # Lightweight discovery of local skills
|-- loader.py            # Progressive loading of SKILL.md and related files
|-- command_planner.py   # Canonical SkillCommandPlan derivation
|-- executor.py          # SkillExecutor and runtime handoff boundary
|-- runtime_client.py    # Optional MCP-backed runtime transport client
`-- models.py            # SkillDefinition, LoadedSkill, SkillCommandPlan, SkillRunRecord, runtime target types
```

**Primary flow:**

1. `SkillRegistry` exposes a lightweight catalog to the planner.
2. `SkillLoader` loads the selected skill only when needed.
   The initial document fetch should focus on full `SKILL.md`; related files are fetched later only if that document makes them relevant.
3. `SkillCommandPlanner` derives a canonical `SkillCommandPlan` from the full `SKILL.md`, not only lightweight catalog metadata.
4. `SkillExecutor` passes the plan through the runtime boundary and returns canonical `run_status`.

When the runtime is MCP-backed and Docker-hosted, the runtime client should prefer host-visible workspace paths in returned payloads whenever it can resolve the container bind mount. Container-local paths may still appear inside `runtime.container_workspace` for debugging and fallback logic.

---

## 3. Canonical Behavior

Skills are intentionally separate from both native tools and external MCP capabilities.

- The planner sees a local skill catalog, not full runtime payloads.
- The executor boundary hides whether the underlying runtime client is local or MCP-backed.
- The runtime client is responsible for normalizing host-visible workspace paths and preserving container workspace metadata when both views are needed.
- `SkillCommandPlan` is the source of truth for runtime target, shell mode, generated files, entrypoint, CLI args, bootstrap commands, and failure reasons.
- `SkillRunRecord` is the canonical lifecycle record; compatibility `status` fields still exist only for older callers.

This layer is shell-oriented by design:

- documented scripts may be run directly
- inline shell or Python can be promoted into generated files
- generated scripts can declare entrypoints and CLI args
- bounded bootstrap commands can prepare a workspace when needed
- free-shell planning should treat natural-language dependency/setup guidance anywhere in the full `SKILL.md` as usable planning input; do not require a dedicated `Dependencies` heading or frontmatter-only metadata before planning bootstrap
- when a utility LLM is available, the planner may first infer missing structured constraints from fuzzy user language before conservative command synthesis
- relative date phrases in user requests can be normalized into explicit CLI dates for shipped scripts when the mapping is unambiguous
- multi-script skills may still auto-lock to one documented shipped script when the inferred CLI contract is clear enough for conservative execution
- narrow skill-specific deterministic fallbacks may exist for high-value artifact skills, but they are exceptions rather than the general dependency/bootstrap strategy

---

## 4. Modification Guidance

- Keep skill execution behind `SkillExecutor`; do not reintroduce ad hoc helper-tool chains inside `AgentCore`.
- Preserve the distinction between planning and execution. `command_planner.py` decides the command shape; runtime clients execute it.
- Keep `run_status` as the internal source of truth. Compatibility-facing `status` strings should not drive new logic.
- Keep generated-script support bounded and transport-aware; avoid pushing large multi-file payloads through public transport surfaces when workspace staging is better.
- Preserve host-workspace normalization, shared-output recovery, and artifact-preview behavior in `runtime_client.py`; do not regress Docker bind-mount path recovery when reshaping runtime payloads.
- Keep the free-shell planner on the full `SKILL.md` path. Do not reintroduce prompt-time truncation or section-only extraction that can drop late dependency/setup instructions.
- Keep document access progressive. Do not turn the first skill-doc read back into a bulk payload of references, file inventory, and planner hints when those files can be fetched explicitly later.
- Keep runtime/tool-result payloads compact. `SkillExecutor` and MCP runtime responses should surface goal, constraints, run status, blocker summaries, artifacts, and small previews; they should not round-trip full file inventories, shell-hint dumps, or planner doc bundles back into the agent context.
- Keep runtime-target and shell-mode logic explicit in the models rather than inferring them from arbitrary strings at the last minute.
- When improving shipped scripts, prefer making the script contract more inferable rather than weakening planner safety checks.
- Treat regex/date heuristics as fallback normalization only; the preferred path for fuzzy-but-low-risk parameter inference is schema-bounded LLM output validated back into typed constraints.

---

## 5. Known Limitations And TODOs

- Local skill execution is shell-oriented and bounded. It does not implement an open-ended autonomous shell agent or a full environment-provisioning system.
- Large generated-script payloads are transport-compacted; very large multi-file bundles are still better staged in the runtime workspace than round-tripped through MCP or API payloads.
- Compatibility `status` is still exposed at some boundaries for older clients, but internal logic should treat canonical `run_status` as authoritative.
- Runtime-backed bootstrap and repair behavior is intentionally bounded. Dependency/setup recovery is primarily LLM-planned from the full `SKILL.md` plus runtime hints rather than from a deterministic parser over free-form prose.
- Relative date parsing still has a deterministic fallback path, but richer business semantics such as fiscal periods, market-specific calendars, or vague anchor phrases still depend on LLM inference quality and validation rules.
- Host-side artifact fallback depends on Docker bind mounts that expose the runtime workspace or shared output root back to the host. Named volumes or remote workers still limit what the client can recover directly from the filesystem.
- Artifact previews are intentionally small and selective; large binary outputs or large tables are still represented mainly through artifact metadata plus small previews.
