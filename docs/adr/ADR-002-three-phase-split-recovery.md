# ADR-002: Three phases with split recovery models

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #6 (phase 3 never compensates)
- **Baseline:** architecture v0.1

## Context

The standard saga pattern assumes **backward recovery**: every forward step has a
compensating inverse, and failure unwinds the transaction. Deletion breaks this
assumption at its root — `DELETE` has no inverse. Once data is purged or a key is
shredded, backward recovery is off the table permanently.

Treating the whole erasure as one compensatable saga therefore encodes a lie: it
implies a `restore` is always available, when past the point of purge it is not.
Worse, a compensating transaction that "restores" a subject after a failed hard
delete would **recreate the subject's data** — converting a failed erasure into an
active breach.

## Decision

Model erasure as **three phases with materially different recovery semantics**,
separated by the human approval gate:

| Phase | Operations | Recovery |
|---|---|---|
| 1 · Discover | read-only inventory, legal-hold check | trivially reversible |
| 2 · Soft delete | disable, tombstone, mark pending | **backward** — compensatable via `restore` |
| — approval gate + grace window — | | |
| 3 · Hard delete | purge, crypto-shred | **forward only** — retry to success, DLQ + runbook |

The transition from backward to forward recovery **at the approval gate** is the
single structural decision the rest of the architecture follows from. Phase 2
failure unwinds everything and fails safe. Phase 3 failure never unwinds: it
retries, routes to a DLQ, and halts.

## Consequences

- **Positive.** The dangerous, irreversible operations are quarantined behind an
  explicit human gate and a grace window, and are the only ones with no rollback.
- **Positive.** Each phase maps cleanly onto a distinct Cedar posture and a distinct
  set of chaos tests (phase-2 failure → full compensation; phase-3 failure → halt).
- **Cost / hazard.** "Phase 3 never compensates" is counter-intuitive and must be
  actively defended — the reflex to "roll back on error" is exactly wrong here.

### Enforcement

- `restore` must be unreachable from any phase-3 code path; a test asserts this
  (invariant #6). `saga/compensate.py` is phase-2 only.

## Alternatives considered

- **Single saga with compensation throughout.** Rejected: assumes an inverse for
  `hard_delete` that does not exist; the "compensation" is data resurrection.

## References

- ARCHITECTURE.md §5 (three-phase saga), §5.1 (phase boundary), §12 (failure matrix), §15 (ADR-002)
- [ADR-006](ADR-006-approval-binds-to-digest.md), [ADR-007](ADR-007-crypto-shredding-for-worm.md)
- Diagram: [04-recovery-semantics](../diagrams/04-recovery-semantics.mermaid)
