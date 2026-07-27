# Architecture Decision Records

This directory holds the ADRs referenced throughout the repo. Each record captures
one decision, the context that forced it, and the alternative that was rejected.
CLAUDE.md's rule stands: **if a change conflicts with an ADR, write a superseding
ADR rather than silently diverging** — the ADR set (including the superseded ones)
is a large part of what makes this repo worth reading.

> **Provenance.** Records 001–014 were backfilled to match the decision log tabulated in
> [`../ARCHITECTURE.md` §15](../ARCHITECTURE.md#15-architecture-decision-records). Records
> 015–020 were written with the v0.2 AWS-native serverless baseline and are the reason
> several earlier records now carry a superseded or refined status. Where ARCHITECTURE.md
> and an ADR disagree, ARCHITECTURE.md §15 is the source of truth and the ADR is the drift.
> Dates are intentionally omitted — the ordering that matters is the supersession chain,
> not the calendar.

## Index

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-agent-proposes-saga-disposes.md) | Agent proposes, saga disposes | Accepted |
| [002](ADR-002-three-phase-split-recovery.md) | Three phases, split recovery models | Accepted |
| [003](ADR-003-step-functions-owns-durability.md) | Step Functions owns durability | **Superseded by [014](ADR-014-langgraph-owns-durability.md)** |
| [004](ADR-004-uniform-five-verb-contract.md) | Uniform 5-verb participant contract | Accepted |
| [005](ADR-005-cedar-at-gateway.md) | Cedar at the Gateway as the control boundary | Accepted — refined by [018](ADR-018-agentcore-policy.md) |
| [006](ADR-006-approval-binds-to-digest.md) | Approval binds to the manifest digest | Accepted |
| [007](ADR-007-crypto-shredding-for-worm.md) | Crypto-shred for WORM participants | Accepted (legal sign-off is a release gate; mechanism moved to the DEK layer by [017](ADR-017-real-aws-participants.md)) |
| [008](ADR-008-recall-1.0-hard-gate.md) | Recall = 1.0 as a hard gate | Accepted — refined by [020](ADR-020-deployed-eval-gate.md) |
| [009](ADR-009-crewai-plus-langgraph.md) | CrewAI + LangGraph split | **Superseded by [011](ADR-011-strands-single-framework.md)** |
| [010](ADR-010-dynamodb-s3-object-lock-ledger.md) | DynamoDB + S3 Object Lock for the ledger | Accepted |
| [011](ADR-011-strands-single-framework.md) | Strands as the single framework | **Superseded by [013](ADR-013-langgraph-single-framework.md)** |
| [012](ADR-012-simulated-participants.md) | Fictional subsystems, not real cloud services | **Superseded by [017](ADR-017-real-aws-participants.md)** |
| [013](ADR-013-langgraph-single-framework.md) | LangGraph as the single framework | Accepted |
| [014](ADR-014-langgraph-owns-durability.md) | LangGraph checkpointers own durability (Aurora + Fargate) | **Superseded by [016](ADR-016-serverless-durability.md)** |
| [015](ADR-015-serverless-compute-split.md) | AgentCore Runtime for reasoning, Lambda for the saga | Accepted |
| [016](ADR-016-serverless-durability.md) | DynamoDB checkpointer + EventBridge Scheduler own durability | Accepted |
| [017](ADR-017-real-aws-participants.md) | Real AWS participants behind AgentCore Gateway | Accepted |
| [018](ADR-018-agentcore-policy.md) | AgentCore Policy is the Cedar runtime | Accepted |
| [019](ADR-019-agentcore-memory-priors.md) | AgentCore Memory holds topology priors, never subject data | Accepted |
| [020](ADR-020-deployed-eval-gate.md) | The recall gate runs against a deployed ephemeral stack | Accepted |
| [021](ADR-021-s3-vectors-for-cost.md) | S3 Vectors replaces OpenSearch Serverless — a cost decision | Accepted — amends [017](ADR-017-real-aws-participants.md), resolves ARCHITECTURE §16 Q7 |
| [022](ADR-022-canonical-json-subset.md) | Canonical JSON is a documented subset of RFC 8785 | Accepted — refines [006](ADR-006-approval-binds-to-digest.md) |
| [023](ADR-023-aurora-needs-a-vpc.md) | Aurora needs a VPC; the platform still never enters one | Accepted — clarifies [015](ADR-015-serverless-compute-split.md), [016](ADR-016-serverless-durability.md), [017](ADR-017-real-aws-participants.md) |
| [024](ADR-024-cedar-expresses-identity-and-shape.md) | Cedar expresses identity and request shape, not business state | Accepted — supersedes the policy set in ARCHITECTURE §9.2, refines [005](ADR-005-cedar-at-gateway.md), [018](ADR-018-agentcore-policy.md) |

## The supersession chains

The repo keeps superseded ADRs on purpose — the fact that the big decisions
*changed*, on the record, is the point.

- **Framework:** [009](ADR-009-crewai-plus-langgraph.md) → [011](ADR-011-strands-single-framework.md) → [013](ADR-013-langgraph-single-framework.md)
- **Durability:** [003](ADR-003-step-functions-owns-durability.md) → [014](ADR-014-langgraph-owns-durability.md) → [016](ADR-016-serverless-durability.md)
- **Participants:** [012](ADR-012-simulated-participants.md) → [017](ADR-017-real-aws-participants.md)

The durability chain is worth reading end to end. It went AWS-owned (Step Functions) →
framework-owned on a database cluster (Aurora + Fargate) → framework-owned on serverless
key-value (DynamoDB + Lambda), and the *principle* — the checkpointer is the system of
record — only appeared at step two and then survived step three unchanged. ADR-016 also
reverses a rejection ADR-014 made explicitly, which is the most useful kind of ADR to read.

## Refinements and amendments

Not every change supersedes. [018](ADR-018-agentcore-policy.md) names the concrete product
behind [005](ADR-005-cedar-at-gateway.md)'s decision; [020](ADR-020-deployed-eval-gate.md)
names where [008](ADR-008-recall-1.0-hard-gate.md)'s gate runs. In both cases the original
decision is untouched and stays Accepted — only the mechanism moved.

[021](ADR-021-s3-vectors-for-cost.md) is the one **amendment**: it changes a single row of
[017](ADR-017-real-aws-participants.md)'s participant table — OpenSearch Serverless → S3
Vectors — for a reason that has nothing to do with the archetype and everything to do with
cost. It is also the only ADR in the set written to **close an open question**: ARCHITECTURE
§16 Q7 asked whether the derived-index archetype was worth a continuous OCU charge, and 021
answers it. The question is marked resolved in place rather than deleted, because a caveat
that got answered is worth more on the record than one that quietly disappears.

## Format

Each ADR uses a short Nygard-style structure: **Status · Context · Decision ·
Consequences · Alternatives considered · References**, plus the CLAUDE.md
invariant(s) it anchors, since the invariants are the enforcement surface for
these decisions.
