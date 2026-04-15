# AGENTS.md - kg_agent/memory

> Back to the root guide: [../AGENTS.md](../AGENTS.md)

---

## 1. Module Positioning

`kg_agent/memory/` owns user- and session-scoped memory surfaces for the agent layer.

It is responsible for:

- In-session conversation storage and context-window selection
- Same-user cross-session retrieval
- User profile persistence and retrieval

It is not responsible for:

- Final answer generation
- Graph retrieval or ingest
- Skill runtime state storage

---

## 2. Key Files And Responsibilities

```text
memory/
|-- conversation_memory.py   # In-session message storage, retrieval, and context-window assembly
|-- cross_session_store.py   # Same-user retrieval across prior sessions, with optional vector backend
`-- user_profile.py          # User profile model and store abstraction
```

**Supported storage intent:**

- `ConversationMemoryStore` supports in-memory behavior plus optional SQLite or Mongo-backed persistence patterns.
- `CrossSessionStore` can work heuristically from stored conversation history, or use an optional Mongo plus Qdrant-style vector path when configured.
- `UserProfileStore` supports lightweight profile persistence without coupling profile logic to the agent loop.

---

## 3. Canonical Behavior

- Session memory is optimized for context-window assembly, not for long-term archival completeness.
- Cross-session retrieval is same-user scoped and is meant to surface relevant prior session snippets into the current run.
- User profiles are separate from message history and are injected as structured context, not reconstructed ad hoc from the whole conversation log every turn.

This separation is intentional:

- session history answers "what happened recently in this thread?"
- cross-session memory answers "what related context exists across the same user's prior sessions?"
- user profile answers "what persistent preferences or identity facts should the agent know?"

---

## 4. Modification Guidance

- Keep memory retrieval lightweight and query-aware. Do not turn the in-session store into a generic document database.
- Preserve the separation between session memory, cross-session memory, and user profile storage instead of merging them into one catch-all store.
- When adding persistence backends, follow the existing abstraction boundaries rather than wiring backend details into `AgentCore`.
- Keep returned memory payloads compact enough to fit prompt budgets; memory is a support surface, not the primary answer payload.

---

## 5. Known Limitations And TODOs

- `CrossSessionStore` supports an optional Mongo plus Qdrant vector backend, but it still falls back to token-overlap and conversation-history heuristics when that backend is absent.
- Cross-session consolidation and background aging remain heuristic. The system does not yet build a long-horizon memory graph or use LLM summarization as the primary compression strategy.
- User profile handling is intentionally lightweight and does not try to infer a large implicit profile from every message.
