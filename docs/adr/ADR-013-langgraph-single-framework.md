# ADR-013: LangGraph as the single framework

- **Status:** Accepted (supersedes [ADR-011](ADR-011-strands-single-framework.md); framework chain [009](ADR-009-crewai-plus-langgraph.md) → [011](ADR-011-strands-single-framework.md) → 013)
- **Anchors invariants:** CLAUDE.md #0 (framework boundary is an explicit allowlist)
- **Baseline:** architecture v0.1

## Context

The framework decision had already changed twice. What forced the third and final
move was a capability shift, not fashion: **LangChain 1.0 middleware** wraps every
tool call, which is precisely the in-process policy pre-check the architecture needs
(§9.1) — the gap that had kept a LangChain-native single stack off the table.
Simultaneously, durability was becoming framework-owned ([ADR-014](ADR-014-langgraph-owns-durability.md)),
and LangGraph's checkpoint/interrupt/resume model is the most mature fit for a saga
that pauses for weeks.

## Decision

**LangGraph is the single framework** for both discovery (a subgraph) and the saga
(a `StateGraph` compiled with a checkpointer). LangChain 1.0 provides agents and
middleware; `ChatBedrockConverse` (langchain-aws) is the model client; MCP
participants are surfaced via `langchain-mcp-adapters`.

Crucially, the framework is confined to an **explicit import allowlist** —
`discovery/`, `saga/`, `policy/middleware.py`, `approval/gate.py`,
`scheduler/handler.py` — enforced by a unit test that names the list verbatim
(invariant #0). `contract/`, `manifest/`, `participants/`, `ledger/` and the policy
*engine* stay framework-free.

## Consequences

- **Positive.** One dependency set, one mental model. The saga is unit-testable in
  plain Python because executor nodes hold no model client ([ADR-001](ADR-001-agent-proposes-saga-disposes.md)).
- **Positive — cheap to reverse a *third* time.** Because framework imports are
  boundaried, the migrations 009→011→013 touched almost nothing. Widening the
  allowlist is an architectural decision, not a convenience import.
- **Cost.** LangGraph now owns durability and its hazards — see [ADR-014](ADR-014-langgraph-owns-durability.md).

## Alternatives considered

- **Strands / dual stack.** Rejected: [ADR-011](ADR-011-strands-single-framework.md)
  predates the LangChain 1.0 middleware capability; the dual stack ([ADR-009](ADR-009-crewai-plus-langgraph.md))
  reintroduces the seam at the manifest handoff.

## References

- ARCHITECTURE.md §6.2 (framework roles), §15 (ADR-013)
- CLAUDE.md invariant #0 · [ADR-014](ADR-014-langgraph-owns-durability.md)
