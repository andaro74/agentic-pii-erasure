# ADR-017: Real AWS participants behind AgentCore Gateway

- **Status:** Accepted (supersedes [ADR-012](ADR-012-simulated-participants.md))
- **Anchors invariants:** CLAUDE.md #5 (never log raw PII), #7 (participants report residuals honestly), #8 (recall gates the build)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-012](ADR-012-simulated-participants.md) made the eight participant subsystems **fictional** — MCP servers over JSON files on the local filesystem — to get hermetic, free, credential-free CI and a `make demo-offline` that anyone could run in thirty seconds. That was a defensible trade for a repo whose merge gate must not depend on a cloud service being reachable.

It bought the wrong thing. The eight archetypes exist to teach eight specific, hard-won lessons, and **every one of those lessons lives in the semantics of a real service**:

- A delete marker is not a deletion — that is S3 versioning behaviour, and a JSON store reproduces it only if you already knew to write it that way.
- A GSI lags its base table — that is DynamoDB, and a dict lookup is always consistent.
- There is no delete-by-query, so a store you cannot enumerate is a store you cannot erase — that is S3 Vectors, and a fake would have offered a convenient `delete_where()`.
- COMPLIANCE-mode Object Lock cannot be deleted from by the root account — that is the entire point of archetype 5, and it is unenforceable in a file.
- `kms:ScheduleKeyDeletion` has a 7-day minimum window — a constraint that *changed the design* (see below), and one no simulation would ever have surfaced.

A simulation reproduces the behaviours its author already understood. It cannot surface the ones they did not, which is exactly the class of failure a reference architecture is supposed to be worth reading for.

The platform is also now AWS-only and serverless by requirement. There is no local mode to protect.

## Decision

The eight participants are **real AWS services**, one per archetype, each fronted by a Lambda function registered as an **AgentCore Gateway target** implementing the five-verb contract.

| # | `systemId` | AWS service | Archetype |
|---|---|---|---|
| 1 | `cognito-identity` | Amazon Cognito | Authoritative identity |
| 2 | `profile-store` | DynamoDB (+ GSIs) | Operational NoSQL |
| 3 | `billing-ledger` | Aurora PostgreSQL Serverless v2 via **RDS Data API** | Relational + FK |
| 4 | `upload-bucket` | S3 with versioning | Deletable blob |
| 5 | `compliance-archive` | S3 Object Lock COMPLIANCE + KMS | **WORM** |
| 6 | `vector-index` | **S3 Vectors** (was OpenSearch Serverless — [ADR-021](ADR-021-s3-vectors-for-cost.md), on cost) | Derived index |
| 7 | `analytics-lake` | S3 + Glue + Athena (Iceberg) | Columnar analytics |
| 8 | `notify-suppression` | Amazon SES suppression list | Residual by design |

Everything is serverless and **nothing requires a VPC** — Aurora is reached through the RDS Data API specifically so the participant Lambda needs no ENI attachment.

Ground truth remains **generated, not labelled**: `evals/fixtures/generator.py` writes synthetic subjects into the real services and emits the placement map in the same pass. That property — not the locality of the data — is what made the recall gate trustworthy, and it survives intact ([ADR-020](ADR-020-deployed-eval-gate.md)).

The seeded data is still entirely made up, and it is still treated exactly as if it were real: pseudonymous `subjectRef` in every log, trace, ledger entry, and memory write (invariant #5). Nothing about moving to real services relaxes that.

## What the real services already taught us

One design change came directly out of building against the real API rather than a fake:

**Crypto-shred happens at the DEK layer, not the CMK layer.** `kms:ScheduleKeyDeletion` enforces a **minimum 7-day pending window** that cannot be shortened. Shredding by destroying a KMS key would mean `hard_delete` could never return `APPLIED` — it would return `PARTIAL` with a 7-to-30-day residual, and the Certificate of Erasure would be unissuable inside a one-month statutory deadline. So objects are encrypted under a per-subject DEK wrapped by a tenant CMK, the wrapped DEK lives in a DynamoDB registry with PITR disabled, and the shred is a registry item delete: immediate and irreversible. [ADR-007](ADR-007-crypto-shredding-for-worm.md)'s decision stands; its *mechanism* moved down a layer because of a real service constraint.

## Consequences

- **Positive — the archetypes are real.** Every lesson in ARCHITECTURE §4.2 is demonstrable against the service that actually behaves that way.
- **Positive — conformance means something.** A conformance suite run against a mock proves the mock conforms. Run against the deployed stack, it proves the participant handles delete markers, GSI lag, Object Lock, and suppression-list retention correctly.
- **Positive — residual honesty gets teeth.** `notify-suppression` genuinely cannot delete the suppression hash; `analytics-lake` genuinely retains rows until snapshot expiry. `PARTIAL` + `residual` stops being a modelled behaviour and becomes an observed one.
- **Cost 1 — CI is no longer hermetic, and this is a real regression.** Conformance, integration, and the recall gate need an AWS account, credentials, and money. Mitigation: an ephemeral per-PR stack created, seeded, evaluated, and destroyed in one workflow. Unit tests, the policy engine, canonicalisation, reducers, and `cdk synth` stay hermetic and remain `make check`.
- **Cost 2 — real spend.** ~~OpenSearch Serverless charges an OCU minimum continuously while the collection exists, and it dominates the bill.~~ **Amended by [ADR-021](ADR-021-s3-vectors-for-cost.md):** participant #6 moved to S3 Vectors purely to remove that floor, and no component now bills continuously for existing rather than for working. Spend is real but proportional to use; an idle stack costs cents. `make destroy-dev` still matters — for the Object Lock teardown constraint in Cost 4, not for the bill.
- **Cost 3 — slower feedback.** A conformance run is minutes, not seconds. `moto` covers participant handler *logic* in unit tests so the fast loop stays fast — but moto is never a substitute for a gate, because the interesting failures are precisely the ones it does not model.
- **Cost 4 — teardown is a correctness concern, not just a cost one.** An Object Lock COMPLIANCE bucket cannot be emptied before its retention expires. Dev stacks use a short retention period and `infra/README.md` says so in the first paragraph.

## Alternatives considered

- **Fictional subsystems** ([ADR-012](ADR-012-simulated-participants.md)). Rejected for the reasons above. Kept on the record because hermetic CI was a genuine benefit and losing it is a genuine cost.
- **LocalStack.** Rejected: it is a simulation with a marketing department. Its Object Lock, KMS deletion windows, and OpenSearch semantics are approximations, so it inherits every weakness of ADR-012 while adding a dependency and a container runtime.
- **Fictional subsystems deployed as Lambdas.** Rejected: serverless and AWS-only, and much cheaper — but the participants would still be simulations wearing a Gateway target, which is the thing this ADR is about.
- **Mixed — real for the hard archetypes only.** Rejected: two classes of participant means two classes of conformance test and a permanent "is this one real?" question on every finding.

## References

- ARCHITECTURE.md §4.2 (archetypes), §11.2 (ground truth), §14 (deployment and cost), §16 Q7
- Supersedes [ADR-012](ADR-012-simulated-participants.md) · [ADR-007](ADR-007-crypto-shredding-for-worm.md), [ADR-008](ADR-008-recall-1.0-hard-gate.md), [ADR-020](ADR-020-deployed-eval-gate.md)
