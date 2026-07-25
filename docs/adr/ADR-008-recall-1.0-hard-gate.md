# ADR-008: Recall = 1.0 is a hard gate

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #8 (recall gates the build), #10 (reducers are a correctness surface)
- **Baseline:** architecture v0.1

## Context

For a deletion agent, output "quality" is nearly irrelevant; **discovery recall is
the safety-critical metric**, because the error modes are asymmetric:

- A **false positive** (a system flagged that holds nothing) is caught by the human
  approver. Cost: reviewer time — about thirty seconds.
- A **false negative** (a system holding subject data is missed) is caught by
  *nobody*. Cost: an undetected regulatory violation, surfacing at audit or breach.

There is no principled threshold beneath 1.0 for a legal completeness obligation:
deleting 7 of 8 systems is not 87% success, it is a reportable breach with a clean
audit trail that says otherwise.

## Decision

**Recall SLO = 1.0.** `make eval` hard-fails below recall 1.0. Precision is tracked
and reported, but **never traded against recall**. When the gate goes red, the fix
is a better discovery agent or a new fixture — **never a lowered threshold**.

Ground truth is *generated, not labelled*: the fixture generator emits the
ground-truth placement map in the same pass it writes the synthetic data
([ADR-012](ADR-012-simulated-participants.md)), so the gate is trustworthy and
hermetic in CI.

A subtle contributor: `saga/state.py` reducers decide how concurrent node writes
merge. A wrong reducer (last-write-wins on a collection) makes two participants'
discovery results silently overwrite each other — which surfaces as a **recall
failure, not a crash**. Every reducer therefore carries a concurrent-write test
(invariant #10).

## Consequences

- **Positive.** The one metric that must not move is defended by an executable gate
  that cannot be quietly relaxed.
- **Cost.** Achieving 1.0 offline demands a deterministic stub model and a carefully
  awkward fixture distribution; both are real engineering, not free.

## Alternatives considered

- **Weighted F1 / a tuned threshold.** Rejected: trades away recall, the only metric
  whose failures are invisible.

## References

- ARCHITECTURE.md §11 (evaluation), §10.1 (metrics), §15 (ADR-008)
- [ADR-012](ADR-012-simulated-participants.md)
