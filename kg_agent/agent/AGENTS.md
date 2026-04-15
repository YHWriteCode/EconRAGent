# AGENTS.md - kg_agent/agent

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/agent/` owns the agent loop and the planner-facing orchestration surface.

It is responsible for:

- `AgentCore` conversation execution
- Route planning through `RouteJudge`
- Optional graph-path explanation through `PathExplainer`
- Native tool registration and planner-visible capability exposure
- Prompt template ownership for routing, explanation, and final answers

It is not responsible for:

- Implementing the native tool business logic itself beyond assembly glue
- Crawl4AI integration details
- Skill runtime execution internals
- Memory backend persistence logic

---

## 2. Key Files And Responsibilities

```text
agent/
|-- agent_core.py           # Main chat loop, direct invoke paths, tool/skill execution orchestration
|-- route_judge.py          # RouteDecision and ToolCallPlan planning
|-- path_explainer.py       # Path and evidence selection plus optional explanation generation
|-- capability_registry.py  # Planner-visible capability metadata and MCP/native registration
|-- tool_registry.py        # Native tool registration and execution
|-- builtin_tools.py        # Builds the default native tool set
|-- prompts.py              # Route-judge, path-explainer, skill-planner, and final-answer prompts
`-- tool_schemas.py         # JSON Schema payloads for native tools
```

**Primary interfaces:**

- `AgentCore.chat()` is the only conversation entry point.
- `AgentCore.invoke_capability(...)` bypasses the planner and executes one capability directly.
- `AgentCore.invoke_skill(...)` bypasses the planner and executes one local skill directly.
- `RouteJudge.plan(...)` returns the structured execution plan.
- `PathExplainer.explain(...)` is an internal augmentation module, not a standalone agent.

---

## 3. Canonical Behavior

The normal flow is:

1. Resolve the active `LightRAG` instance, often through a dynamic provider.
2. Load session context, user profile, enabled capabilities, and the local skill catalog.
3. Ask `RouteJudge` for a structured decision.
4. Execute native tools through `ToolRegistry` and external capabilities through `MCPAdapter`.
5. Execute a local `skill_plan` through `SkillExecutor` when the route emits one.
6. Optionally trigger path explanation and dynamic-graph refresh behavior.
7. Build the final answer prompt and persist compact memory records.

Planner surfaces are intentionally split:

- `ToolRegistry` is the execution registry for native built-in tools.
- `CapabilityRegistry` is the planner and API-visible catalog of native and external capabilities.
- `SkillRegistry` is a separate planner-facing catalog for local skills.

Skill auto-routing quality depends on the catalog metadata the planner sees:

- local skills should publish clear names, descriptions, and tags
- domain skills should include strong bilingual keywords when the user may query in either Chinese or English

Keep those layers distinct. A local skill is not a planner-visible MCP tool, and a native tool is not the same thing as a capability record.

---

## 4. Modification Guidance

- Do not hardcode tool orchestration logic in `agent_core.py`; add or change tools through `ToolRegistry`, `CapabilityRegistry`, and routing rules instead.
- Do not expose `RouteJudge` or `PathExplainer` as standalone chat endpoints.
- Keep framework-reserved tool kwargs under framework control; tool-generated args must not override values such as `query`, `rag`, `session_id`, `user_id`, `session_context`, `user_profile`, `domain_schema`, or `memory_store`.
- Keep prompt ownership centralized in `prompts.py`; do not scatter prompt templates across unrelated modules.
- Keep native capability exposure lightweight and metadata-driven. Domain-specific external systems should usually be surfaced through MCP rather than hardcoded into the native registry.
- Keep path explanation optional and fallback-safe. Plain answers must still work when graph paths or evidence are weak.

---

## 5. Known Limitations And TODOs

- Configured `external_mcp` capabilities are hidden from auto-routing unless `planner_exposed=true`, even though explicit invocation already works.
- Path explanation already consumes schema-provided explanation profiles, relation semantics, guardrails, and scenario overrides, but only the built-in `general` and `economy` profiles exist today.
- Path explanation still uses lightweight lexical and semantic-token scoring; embedding-based reranking is not integrated yet.
- Utility-model fallback is supported for routing and explanation work, but richer planner-specialized model strategies are still out of scope for this layer.
