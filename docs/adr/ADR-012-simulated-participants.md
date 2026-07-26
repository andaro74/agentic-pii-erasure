# ADR-012: Fictional subsystems, not real cloud services

- **Status:** **Superseded by [ADR-017](ADR-017-real-aws-participants.md)**
- **Anchors invariants:** CLAUDE.md #5 (never log raw PII), #8 (recall gates the build)
- **Baseline:** architecture v0.1 (kept deliberately — hermetic CI was a real benefit, and losing it is a real cost)

> **Why it was superseded.** The eight archetypes exist to teach lessons that live in the
> semantics of real services — S3 delete markers, DynamoDB GSI lag, COMPLIANCE-mode Object
> Lock, the 7-day `kms:ScheduleKeyDeletion` floor. A simulation reproduces the behaviours its
> author already understood and cannot surface the ones they did not.
> [ADR-017](ADR-017-real-aws-participants.md) moves the participants onto real AWS services;
> [ADR-020](ADR-020-deployed-eval-gate.md) records where the recall gate runs now, and what
> giving up hermetic CI costs.

## Context

The reference implementation needs eight participant systems to demonstrate the
five-verb contract and to feed the recall gate. Wiring those to real AWS services
(or LocalStack / Docker Compose) makes CI slow, costly, credential-bound, and
non-hermetic — and a merge gate must not depend on a cloud service being reachable
(§11.2).

There is also a subtler requirement: the recall gate is only trustworthy if ground
truth is *not hand-labelled*. Labels drift from reality; a generator that writes the
data and records where it wrote it cannot.

## Decision

The eight subsystems are **fictional** — real MCP servers over JSON state on the
local filesystem, one per archetype. Everything is fake data; there is no real user
data in the repo, ever. `make demo-offline` needs no cloud account and costs nothing.

Ground truth is **generated, not labelled**: `evals/fixtures/generator.py` writes
synthetic subjects into a deliberately awkward distribution and **emits the
ground-truth placement map in the same pass** it writes the data. Discovery then
runs blind, and recall is computed against the map ([ADR-008](ADR-008-recall-1.0-hard-gate.md)).

The production mapping for each simulated component (SQLite → Aurora, in-process
timers → EventBridge Scheduler, JSONL ledger → DynamoDB + Object Lock, etc.) is
tabulated in the README so the simulation boundary is explicit, not hidden.

## Consequences

- **Positive.** Hermetic, free, deterministic CI; the recall gate is trustworthy
  because generated-not-labelled ground truth cannot silently disagree with the data.
- **Positive.** The seeded fake PII is treated exactly as if it were real — the same
  redaction discipline (invariant #5) applies, which is itself part of the demo.
- **Cost / honesty.** What is simulated must be stated plainly (README "What is
  deliberately simulated"); dressing a local simulation up as a live cloud deployment
  would be the dishonesty this ADR exists to avoid.

## Alternatives considered

- **Real AWS / LocalStack / Docker Compose.** Rejected: slow, costly, non-hermetic,
  credential-bound; and none of them give generated ground truth for free.

## References

- ARCHITECTURE.md §11.2 (ground truth by construction), §14 (cost note), §15 (ADR-012)
- README "What is deliberately simulated" · [ADR-008](ADR-008-recall-1.0-hard-gate.md)
