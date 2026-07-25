# ADR-006: Approval binds to the manifest digest, not the subject

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #3 (approval binds to manifest digest), #4 (canonicalisation must be byte-stable)
- **Baseline:** architecture v0.1

## Context

A human approves a specific plan. But discovery is non-deterministic and re-runnable,
which opens a time-of-check/time-of-use hole:

**Attack.** The approver reviews manifest v1 — three low-risk systems. Between
approval and execution the agent re-discovers and produces v2, which now includes
the production customer database. Execution proceeds under v1's approval. The human
authorized something they never saw.

Keying the approval on `subject_ref` alone does not close this — the subject is the
same across v1 and v2.

## Decision

The approval token is cryptographically bound to `sha256(canonical(manifest))`. Any
code path that mints, validates, or consumes an approval token carries
`manifest_digest`. Cedar enforces `approvalToken.manifestDigest == manifestDigest`
on every phase-3 call. **Any** change to the plan — even reordering — changes the
digest and invalidates the approval, forcing re-review.

**Manifests are immutable after signature.** Re-planning produces a *new* manifest
and a *new* approval cycle; you never edit an approved manifest and execute under
the prior token.

This makes canonicalisation load-bearing: the digest binding is only as strong as
`contract/canonical.py`. Semantically identical plans must produce identical
bytes — sorted keys, normalised numbers, deterministic array order, and **no
timestamps or run IDs in the digested body** — or approvals churn and the binding
becomes unusable ([ADR-006](ADR-006-approval-binds-to-digest.md) depends on invariant #4).

## Consequences

- **Positive.** Closes the plan-substitution TOCTOU (threat T2). The approver's
  decision provably applies to the exact bytes executed.
- **Positive.** Provenance (timestamps, run IDs, trace IDs) can change freely without
  churning the digest, because it is excluded from the digested body.
- **Cost.** Any change to canonicalisation is a **breaking change**: bump
  `schemaVersion` and add a fixture. This is deliberate friction.

## Alternatives considered

- **Approval bound to subject ID.** Rejected: does not detect plan growth between
  approval and execution.

## References

- ARCHITECTURE.md §8.3 (approval binds to the plan), §7.1 (manifest), §15 (ADR-006)
- [ADR-001](ADR-001-agent-proposes-saga-disposes.md), [ADR-005](ADR-005-cedar-at-gateway.md)
