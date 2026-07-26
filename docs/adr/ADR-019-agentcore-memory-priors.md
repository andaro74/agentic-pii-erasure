# ADR-019: AgentCore Memory holds topology priors, never subject data

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #5 (never log raw PII), #13 (Memory is topology-only)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

Discovery is the expensive part of an erasure. It is also the part that should get cheaper with experience: after ten deletions in a tenant, the agent ought to already know that this tenant's `vector-index` mirrors the profile store, and that `analytics-lake` always holds a copy. Earlier drafts put those priors in a DynamoDB table the agent read and wrote directly.

That works, but it means hand-building retrieval, scoping, consolidation, and expiry for something **AgentCore Memory** provides natively — with per-actor and per-session scoping, long-term memory extraction, and semantic retrieval — and the reasoning plane already runs on AgentCore Runtime, which integrates with it directly.

The hazard is equally clear. Memory is a *cross-subject* surface by design: its whole value is that something learned while deleting subject A improves the deletion of subject B. Any PII that lands there leaks across the exact boundary the architecture is built to protect (threat T7).

## Decision

**Topology priors live in AgentCore Memory, scoped per tenant. Subject data never does.**

What may be written:

- Which systems this tenant has historically held subject data in
- Derived-store relationships (`vector-index` mirrors `profile-store`)
- Which `scopeHints` proved productive for this tenant
- Systems that consistently return `found: false` and can be deprioritised (never skipped — see below)

What may never be written, in any form:

- `subjectRef`, or anything derivable from it
- Artifact locators, counts, or classifications for a specific subject
- Hold identifiers, approver identities, manifest digests
- Any raw PII, obviously, but the rule is stricter than that: **no per-subject facts at all**

Three enforcement layers, because a convention is not a control:

1. **A pre-write scrubber.** Every write goes through `observability/redact.py`; a write containing a subject-shaped token is rejected, not sanitised. Failing loudly beats silently storing a near-miss.
2. **The `no_pii_in_memory` evaluator.** Reads back the tenant's memory after an eval run and fails the build on any subject-shaped content. This is the control that can actually go red.
3. **Separation from the checkpointer.** Checkpoints legitimately contain a full manifest of artifact locators and live in DynamoDB ([ADR-016](ADR-016-serverless-durability.md)). Two stores with two rules means there is never an ambiguous case about which one may hold subject-shaped data. This is why `AgentCoreMemorySaver` was rejected as a checkpoint backend.

**Priors are advisory, never authoritative.** A prior may reorder or prioritise discovery. It may never cause a system to be skipped, and it may never satisfy a `discover` call. Recall is measured against ground truth with priors both cold and warm; if a warm prior ever lowers recall, that is a P1 and the prior is wrong, not the gate ([ADR-008](ADR-008-recall-1.0-hard-gate.md), invariant #8).

## Consequences

- **Positive — discovery cost decreases with tenant experience** without hand-rolling a retrieval layer.
- **Positive — the PII boundary is a testable assertion**, not a code-review habit.
- **Positive — clean separation of concerns.** Memory is what the agent *learned*; the checkpointer is what the saga *is*. Neither can be mistaken for the other.
- **Cost 1 — a genuine leak surface.** The scrubber is the only thing between a careless write and a cross-subject PII leak. It gets the same test discipline as `contract/canonical.py`.
- **Cost 2 — priors can encode a stale topology.** A tenant that decommissions a system leaves a prior pointing at it. Harmless for recall (a false positive is caught by the approver) but it costs discovery time; priors carry a last-confirmed timestamp and decay.
- **Cost 3 — evaluating a warm agent is harder than evaluating a cold one.** The eval harness must control memory state explicitly, or runs are not reproducible. `evals/run.py` resets tenant memory between suites and runs the recall gate both cold and warm.

## Alternatives considered

- **DynamoDB priors table.** Rejected: works, but reimplements scoping, retrieval, and expiry that AgentCore Memory provides, and gains nothing now that the reasoning plane is on AgentCore Runtime.
- **AgentCore Memory as general agent state, including per-subject context.** Rejected outright. It collapses the cross-subject boundary and makes threat T7 structural rather than preventable.
- **No priors at all.** Rejected: discovery cost stays flat forever, and the "gets better with experience" property is one of the few genuinely agentic arguments in this architecture.

## References

- ARCHITECTURE.md §6.4 (topology priors), §7.2 (stores), §9.5 T7, §11.3 (`no_pii_in_memory`)
- [ADR-008](ADR-008-recall-1.0-hard-gate.md) · [ADR-016](ADR-016-serverless-durability.md) (why Memory is not the checkpointer)
