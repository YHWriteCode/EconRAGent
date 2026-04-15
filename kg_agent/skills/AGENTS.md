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
3. `SkillCommandPlanner` derives a canonical `SkillCommandPlan`.
4. `SkillExecutor` passes the plan through the runtime boundary and returns canonical `run_status`.

---

## 3. Canonical Behavior

Skills are intentionally separate from both native tools and external MCP capabilities.

- The planner sees a local skill catalog, not full runtime payloads.
- The executor boundary hides whether the underlying runtime client is local or MCP-backed.
- `SkillCommandPlan` is the source of truth for runtime target, shell mode, generated files, entrypoint, CLI args, bootstrap commands, and failure reasons.
- `SkillRunRecord` is the canonical lifecycle record; compatibility `status` fields still exist only for older callers.

This layer is shell-oriented by design:

- documented scripts may be run directly
- inline shell or Python can be promoted into generated files
- generated scripts can declare entrypoints and CLI args
- bounded bootstrap commands can prepare a workspace when needed
- relative date phrases in user requests can be normalized into explicit CLI dates for shipped scripts when the mapping is unambiguous
- multi-script skills may still auto-lock to one documented shipped script when the inferred CLI contract is clear enough for conservative execution

---

## 4. Modification Guidance

- Keep skill execution behind `SkillExecutor`; do not reintroduce ad hoc helper-tool chains inside `AgentCore`.
- Preserve the distinction between planning and execution. `command_planner.py` decides the command shape; runtime clients execute it.
- Keep `run_status` as the internal source of truth. Compatibility-facing `status` strings should not drive new logic.
- Keep generated-script support bounded and transport-aware; avoid pushing large multi-file payloads through public transport surfaces when workspace staging is better.
- Keep runtime-target and shell-mode logic explicit in the models rather than inferring them from arbitrary strings at the last minute.
- When improving shipped scripts, prefer making the script contract more inferable rather than weakening planner safety checks.

---

## 5. Known Limitations And TODOs

- Local skill execution is shell-oriented and bounded. It does not implement an open-ended autonomous shell agent or a full environment-provisioning system.
- Large generated-script payloads are transport-compacted; very large multi-file bundles are still better staged in the runtime workspace than round-tripped through MCP or API payloads.
- Compatibility `status` is still exposed at some boundaries for older clients, but internal logic should treat canonical `run_status` as authoritative.
- Runtime-backed bootstrap and repair behavior is intentionally bounded. Automatic dependency classification and richer environment recovery remain lower-priority improvements.
- Relative date parsing is intentionally pragmatic and phrase-based; very domain-specific time semantics still need explicit constraints or richer planner guidance.
