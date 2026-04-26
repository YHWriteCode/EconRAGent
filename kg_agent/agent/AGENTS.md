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
|-- prompts.py              # Route-judge, path-explainer, skill constraint inference, skill-planner, and final-answer prompts
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

When a runtime-backed skill initially returns `run_status=running`, `AgentCore` now performs a short bounded polling loop through the skill runtime status/logs/artifacts APIs before final answer generation. This is meant to collapse fast durable runs into a terminal result for normal `chat()` flows without turning the agent path into an unbounded job waiter.

Two additional routing expectations now matter:

- Questions about the agent's own available tools, skills, or capabilities should route to a no-tool metadata answer path rather than KG retrieval, because the capability catalog and skill catalog are already available in planner/final-answer context.
- Financial market questions such as current stock price, recent行情, volatility, or trend analysis should prefer a matching finance skill when one is present, instead of defaulting to `kg_hybrid_search`.
- Direct artifact-production requests such as creating a PPT / PowerPoint / slides / deck / 简报, or similar spreadsheet/document outputs, should be evaluated against the exposed skill and capability catalogs before any KG retrieval fallback. If a concrete local skill like `pptx`, `xlsx`, or `pdf` is a clear fit, route to `skill_request`.

Route refinement is no longer allowed to casually undo an already concrete skill match:

- When the rule-based layer has already matched a direct-output skill request or an agent-metadata answer, optional LLM refinement should preserve that route instead of downgrading it into `factual_qa` or `kg_hybrid_search`.
- This is especially important for queries whose intent is to produce or manipulate an artifact, because knowledge retrieval is supporting context at most, not the primary execution surface.

The final-answer stage now also receives the capability catalog and skill catalog, not only tool results. That allows direct answers for metadata questions even when no tool is called.

Planner surfaces are intentionally split:

- `ToolRegistry` is the execution registry for native built-in tools.
- `CapabilityRegistry` is the planner and API-visible catalog of native and external capabilities.
- `SkillRegistry` is a separate planner-facing catalog for local skills.

Skill auto-routing quality depends on the catalog metadata the planner sees:

- local skills should publish clear names, descriptions, and tags
- domain skills should include strong bilingual keywords when the user may query in either Chinese or English
- artifact-oriented skills should include output-format aliases and user-facing nouns, for example `ppt`, `powerpoint`, `presentation`, `slides`, `deck`, `简报`, `excel`, `spreadsheet`, or similar terms that users naturally say instead of the raw skill name

Keep those layers distinct. A local skill is not a planner-visible MCP tool, and a native tool is not the same thing as a capability record.

---

## 4. Modification Guidance

- Do not hardcode tool orchestration logic in `agent_core.py`; add or change tools through `ToolRegistry`, `CapabilityRegistry`, and routing rules instead.
- Do not expose `RouteJudge` or `PathExplainer` as standalone chat endpoints.
- Keep framework-reserved tool kwargs under framework control; tool-generated args must not override values such as `query`, `rag`, `session_id`, `user_id`, `session_context`, `user_profile`, `domain_schema`, or `memory_store`.
- Keep prompt ownership centralized in `prompts.py`; do not scatter prompt templates across unrelated modules.
- Keep skill terminal-result waiting bounded and config-driven. Do not replace the short polling path with indefinite blocking waits inside normal chat flows.
- Preserve the distinction between user-domain questions and agent-metadata questions. If the agent already has the relevant capability/skill metadata in context, do not route those questions through KG retrieval just to restate the local catalog.
- Preserve the finance-skill preference for stock/quote/volatility/trend queries when a matching finance skill exists. Do not let the generic factual-QA fallback silently outrank a concrete market-analysis skill.
- Preserve the skill-first rule for direct output requests. Queries like "做一个 PPT", "生成简报", "build a deck", or other explicit artifact requests should not fall through to KG retrieval when a matching local skill is already exposed.
- Keep route locking intentional. If the rule-based planner has already identified a concrete direct-output skill route or an `agent_metadata_answer`, optional LLM refinement should not degrade it into a generic retrieval path.
- Keep the final-answer prompt aligned with the routing surface: if route planning sees capability and skill catalogs, final-answer generation should also receive them when they are needed to answer the user directly.
- When adding LLM-assisted normalization for skills, keep it schema-bounded and prompt-driven; do not let planner prompts devolve into free-form command generation outside the skill runtime boundary.
- Keep native capability exposure lightweight and metadata-driven. Domain-specific external systems should usually be surfaced through MCP rather than hardcoded into the native registry.
- Keep path explanation optional and fallback-safe. Plain answers must still work when graph paths or evidence are weak.
- Keep path role expectations tolerant of schema evolution. For economy geography roles, preserve legacy `country_context` compatibility while accepting the current `location_context` role emitted by the schema profile.

---

## 5. Known Limitations And TODOs

- Configured `external_mcp` capabilities are hidden from auto-routing unless `planner_exposed=true`, even though explicit invocation already works.
- Path explanation already consumes schema-provided explanation profiles, relation semantics, guardrails, and scenario overrides, but only the built-in `general` and `economy` profiles exist today.
- Path explanation still uses lightweight lexical and semantic-token scoring; embedding-based reranking is not integrated yet.
- Utility-model fallback is supported for routing and explanation work, but richer planner-specialized model strategies are still out of scope for this layer.
- Explicit skill invocation APIs may still return an immediate run record for client-managed follow-up; the bounded terminal wait is mainly part of the planner/chat orchestration path.
- Routing still mixes explicit rules with optional LLM refinement. New domain categories may still need additional rule coverage when a generic KG fallback remains too attractive.
