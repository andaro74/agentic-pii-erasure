# ADR-001: The agent proposes, the saga disposes

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #1 (discovery never gets a mutating tool), #2 (deletion tools are called by executor nodes, not models)
- **Baseline:** architecture v0.1

## Context

The obvious decomposition of "use an agent to delete a user" is to give a model
the deletion tools and let it act. That collapses under the first compliance
question every erasure system eventually faces: *"why was this record deleted?"*
"The model decided" is not an answer a regulator, or an on-call engineer at 3am,
can use.

Non-determinism is an asset in exactly one place — **discovery and planning**,
where the participant set is not known at design time and the correct plan varies
per subject and per tenant. Everywhere else it is a liability, and deletion is the
one operation with no undo (see [ADR-002](ADR-002-three-phase-split-recovery.md)).

## Decision

The model never calls a deletion tool. It emits a **signed, versioned Deletion
Manifest** describing what it found and what it intends. Execution is
**deterministic replay** of that manifest by the saga's executor nodes — plain
Python functions with no model client — under policy the model cannot reach
([ADR-005](ADR-005-cedar-at-gateway.md)).

This draws a hard line through the system:

- **Reasoning plane** — reads broadly, writes nothing, is the *least privileged*
  plane in the architecture.
- **Control plane** — replays an approved plan deterministically; never re-enters
  the model.

## Consequences

- **Positive.** Every deletion traces to a signed artifact and an approver's
  identity. Replay is testable in plain Python, so the whole saga can be exercised
  against a hand-written fixture manifest *before discovery exists* (ROADMAP M5).
- **Positive.** The `contract/`, `manifest/`, `participants/` and `ledger/`
  packages stay framework-free; this is what let two framework migrations
  ([009](ADR-009-crewai-plus-langgraph.md) → [011](ADR-011-strands-single-framework.md)
  → [013](ADR-013-langgraph-single-framework.md)) touch almost nothing.
- **Cost.** A round trip through a human-reviewable artifact is slower than letting
  the model act directly. That latency is the price of auditability, and it is the
  right trade for an irreversible action.

### Enforcement

- The discovery subgraph is constructed with `subject.discover`/`subject.verify`
  only; a unit test asserts the tool list at construction (invariant #1).
- `saga/nodes/` may not import a model client; a unit test asserts this (invariant #2).

## Alternatives considered

- **Agent invokes deletions directly.** Rejected: unauditable, and it makes a
  prompt-injection reachable path to `hard_delete` — the exact failure ADR-005
  exists to prevent.

## References

- ARCHITECTURE.md §1.1 (governing principle), §15 (ADR-001)
- [ADR-002](ADR-002-three-phase-split-recovery.md), [ADR-005](ADR-005-cedar-at-gateway.md), [ADR-006](ADR-006-approval-binds-to-digest.md)
