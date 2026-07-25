# ADR-004: A uniform five-verb participant contract

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #7 (participants report residuals honestly)
- **Baseline:** architecture v0.1

## Context

Every system that holds subject data deletes differently — Cognito disables then
deletes, Aurora needs FK-ordered deletes, S3 versioning turns a delete into a
delete *marker*, a WORM bucket has no delete API at all. The naive integration
grows one bespoke code path per system, and the agent's tool surface grows with it.

That last part is the real cost. A tool surface that grows O(N) with participant
count degrades tool-selection accuracy — and tool selection is exactly the metric a
discovery agent cannot afford to lose.

## Decision

Every participating system — regardless of technology — exposes **exactly five MCP
tools**:

```
subject.discover      read-only. What exists for this subject here?
subject.soft_delete   reversible. Disable, tombstone, mark pending.
subject.restore       the compensating transaction for soft_delete.
subject.hard_delete   irreversible. Purge or crypto-shred.
subject.verify        read-only assertion. Must return zero.
```

Adding subsystem #9 means writing one MCP server and passing `make conformance`.
The agent's tool surface stays **O(1) in participant count**. Per-system deletion
semantics live behind the uniform contract, declared by the participant (e.g. FK
ordering in the `discover` response) rather than hardcoded in the orchestrator.

The `residual` field is **mandatory and load-bearing**: a participant that cannot
fully delete returns `PARTIAL` with a populated `residual`, never a hopeful
`APPLIED`. Silent partial success is the failure mode that produces compliance
incidents; the contract makes it unrepresentable.

## Consequences

- **Positive.** Onboarding cost is independent of participant count; conformance
  tests parameterise over the registry, so a new participant is covered automatically.
- **Positive.** The contract types (`contract/`) depend on nothing and are
  framework-free, anchoring the dependency graph.
- **Cost.** Some systems need awkward mappings onto five verbs (WORM's `hard_delete`
  = destroy the DEK; a suppression list's honest residual). That awkwardness is
  *disclosed* rather than hidden — which is the point.

## Alternatives considered

- **Per-system bespoke integration.** Rejected: O(N) tool surface, unauditable
  sprawl, and no shared conformance guarantee.
- **A generic "run this query / call this API" tool.** Rejected explicitly (CLAUDE.md):
  Cedar cannot express a constraint over arbitrary SQL, so blast radius becomes
  unbounded and the policy layer is voided.

## References

- ARCHITECTURE.md §4 (participant contract), §4.4 (conformance), §15 (ADR-004)
- README "The Deletion Participant Contract"
