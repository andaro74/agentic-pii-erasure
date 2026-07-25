# Architecture Decision Records

This directory holds the ADRs referenced throughout the repo. Each record captures
one decision, the context that forced it, and the alternative that was rejected.
CLAUDE.md's rule stands: **if a change conflicts with an ADR, write a superseding
ADR rather than silently diverging** — the ADR set (including the superseded ones)
is a large part of what makes this repo worth reading.

> **Provenance.** These records were backfilled to match the decision log already
> tabulated in [`../ARCHITECTURE.md` §15](../ARCHITECTURE.md#15-architecture-decision-records)
> and the narrative in §6. They are the "architecture baseline v0.1" decisions;
> where ARCHITECTURE.md and an ADR disagree, ARCHITECTURE.md §15 is the source of
> truth and the ADR is the drift. Dates are intentionally omitted — the ordering
> that matters is the supersession chain, not the calendar.

## Index

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-agent-proposes-saga-disposes.md) | Agent proposes, saga disposes | Accepted |
| [002](ADR-002-three-phase-split-recovery.md) | Three phases, split recovery models | Accepted |
| [003](ADR-003-step-functions-owns-durability.md) | Step Functions owns durability | **Superseded by [014](ADR-014-langgraph-owns-durability.md)** |
| [004](ADR-004-uniform-five-verb-contract.md) | Uniform 5-verb participant contract | Accepted |
| [005](ADR-005-cedar-at-gateway.md) | Cedar at the Gateway as the control boundary | Accepted |
| [006](ADR-006-approval-binds-to-digest.md) | Approval binds to the manifest digest | Accepted |
| [007](ADR-007-crypto-shredding-for-worm.md) | Crypto-shred for WORM participants | Accepted (legal sign-off is a release gate) |
| [008](ADR-008-recall-1.0-hard-gate.md) | Recall = 1.0 as a hard gate | Accepted |
| [009](ADR-009-crewai-plus-langgraph.md) | CrewAI + LangGraph split | **Superseded by [011](ADR-011-strands-single-framework.md)** |
| [010](ADR-010-dynamodb-s3-object-lock-ledger.md) | DynamoDB + S3 Object Lock for the ledger | Accepted |
| [011](ADR-011-strands-single-framework.md) | Strands as the single framework | **Superseded by [013](ADR-013-langgraph-single-framework.md)** |
| [012](ADR-012-simulated-participants.md) | Fictional subsystems, not real cloud services | Accepted |
| [013](ADR-013-langgraph-single-framework.md) | LangGraph as the single framework | Accepted |
| [014](ADR-014-langgraph-owns-durability.md) | LangGraph checkpointers own durability | Accepted |

## The two supersession chains

The repo keeps superseded ADRs on purpose — the fact that the big decisions
*changed*, on the record, is the point.

- **Framework:** [009](ADR-009-crewai-plus-langgraph.md) → [011](ADR-011-strands-single-framework.md) → [013](ADR-013-langgraph-single-framework.md)
- **Durability:** [003](ADR-003-step-functions-owns-durability.md) → [014](ADR-014-langgraph-owns-durability.md)

## Format

Each ADR uses a short Nygard-style structure: **Status · Context · Decision ·
Consequences · Alternatives considered · References**, plus the CLAUDE.md
invariant(s) it anchors, since the invariants are the enforcement surface for
these decisions.
