# Agentic PII Erasure

**Automated, auditable, multi-system deletion of a data subject — where the agent never deletes anything.**

A serverless reference implementation of [GDPR Art. 17](https://gdpr-info.eu/art-17-gdpr/) erasure across eight real AWS services, built on [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/), LangGraph, and a three-phase saga.

**Deploys to AWS. There is no local mode.** No servers, no clusters, no VPC, no always-on compute.

```bash
make install && make deploy-dev && make seed && make walkthrough
```

> ⚠️ This spends real money. Read [`infra/README.md`](infra/README.md) first, and run `make destroy-dev` when you are done.

---

## The problem

Deleting a user is presented as a CRUD operation. It is not. It is a **distributed transaction across systems that will never agree to a two-phase commit**, executed against a participant set that **is not known at design time**, with a **legally mandated completeness guarantee** and **no undo**.

Four properties make it genuinely hard:

1. **Unknown participants.** Nobody has an accurate map of which systems hold data for a given subject. CMDBs are stale. Lineage tooling covers the warehouse and not the seventeen services writing into it.
2. **No compensating transaction.** The saga pattern assumes every forward action has an inverse. `DELETE` does not.
3. **Physically undeletable stores.** An S3 Object Lock bucket in COMPLIANCE mode cannot be deleted from by anyone, including the root account. Deletion has to be redefined as *irreversible loss of readability*.
4. **Completeness is binary.** Deleting 7 of 8 systems is not 87% success. It is a reportable breach with a clean audit trail saying otherwise.

## The idea

> **The agent proposes. The saga disposes.**

Non-determinism is an asset in exactly one place — **discovery**, where the search space is open-ended. It is a liability everywhere else.

The model never calls a deletion tool. It produces a **signed, versioned Deletion Manifest**. Execution is deterministic replay of that manifest by Lambda functions whose execution role has **no `bedrock:InvokeModel` permission at all**, gated by Cedar policy the model cannot reach. When a regulator asks *"why was this record deleted?"*, the answer is a signed artifact and an approver's identity — not "the model decided."

## Why serverless is an architectural claim, not a deployment choice

The platform is idle almost all of the time. A saga spends **days to weeks** parked at the approval gate, and **seconds** doing work. Nothing serverless can hold that pause as a process — Lambda caps at 15 minutes, an AgentCore Runtime session at 8 hours — so **the pause has to be data**.

A node calls `interrupt()`, LangGraph writes a checkpoint to DynamoDB, and the Lambda returns. Nothing is running. Days later, an EventBridge Scheduler one-shot fires a resume Lambda on different hardware and the saga continues exactly where it stopped. That is the property worth watching once in the walkthrough.

Two things fall out of it:

- **The checkpointer is forced to be the system of record**, not a cache — there is no warm process for state to hide in.
- **Privilege separation becomes IAM.** Each plane is a different execution role, so "the agent cannot delete" and "the saga cannot think" are enforceable and auditable, not conventions.

## The part most saga write-ups get wrong

Deletion has no inverse, so this is modelled as **three phases with different recovery semantics**:

| Phase | Operations | Recovery |
|---|---|---|
| 1 · Discover | read-only inventory, legal-hold check | trivially reversible |
| 2 · Soft delete | disable, tombstone, anonymise-pending | **backward** — compensatable |
| — **human approval gate + grace window** — | | |
| 3 · Hard delete | purge, crypto-shred | **forward only** — retry to success, DLQ + runbook |

The switch from backward to forward recovery at the approval gate is the single structural decision everything else follows from. See [`docs/diagrams/04-recovery-semantics.mermaid`](docs/diagrams/04-recovery-semantics.mermaid).

---

## The eight participants — all real AWS services

Meridian Outfitters is a made-up multi-brand retailer with fabricated customers. The services holding their data are not simulated: each is a real AWS service behind a Lambda that implements the five-verb contract, registered as an AgentCore Gateway target.

| `systemId` | AWS service | Archetype | The lesson it teaches |
|---|---|---|---|
| `cognito-identity` | Cognito | Authoritative identity | Revoke first — stop new writes before deleting old ones |
| `profile-store` | DynamoDB | Document store | GSI fan-out, and a GSI that lags its base table |
| `billing-ledger` | Aurora Serverless v2 | Relational | FK ordering; statutory financial retention beats erasure |
| `upload-bucket` | S3 (versioned) | Blob store | A delete marker is **not** a deletion |
| `compliance-archive` | S3 Object Lock + KMS | **WORM** | No delete API exists. Crypto-shred or nothing. |
| `vector-index` | S3 Vectors | Derived index | An embedding outlives its source — and *is* personal data |
| `analytics-lake` | Glue / Athena (Iceberg) | Columnar analytics | You cannot delete a row from a Parquet file |
| `notify-suppression` | SES suppression list | Residual by design | The suppression hash **must** stay — disclose it |

`compliance-archive` and `notify-suppression` are the two worth reading first. Neither can honour a deletion request in the way the word implies, and pretending otherwise is how compliance incidents happen.

These used to be eight fictional subsystems over JSON files. [ADR-017](docs/adr/ADR-017-real-aws-participants.md) records why they are not any more, and what hermetic CI cost to give up.

**Building against the real services already changed the design, twice.** `kms:ScheduleKeyDeletion` has a minimum 7-day pending window that cannot be shortened — so a crypto-shred implemented as "destroy the KMS key" could never return `APPLIED` inside a one-month statutory deadline. The shred moved down a layer, to deleting the wrapped per-subject data key from a registry that is deliberately excluded from every backup path. And S3 Vectors has **no delete-by-query**: `DeleteVectors` takes keys and nothing else, so `vector-index` derives its keys deterministically from the pseudonymous subject handle. That turns "keep the identifier alive until last" from a good idea into a hard requirement — lose the join key and the embeddings remain fully present and permanently unaddressable. No simulation would have surfaced either constraint.

## The seven made-up subjects

Each seeded subject exercises a different path. They are the walkthrough *and* the eval fixtures.

| Subject | Scenario | What it proves |
|---|---|---|
| Marisol Okonkwo | Present in 6 of 8, no holds | Happy path, end to end |
| Dmitri Vasquez-Lund | Litigation hold in `billing-ledger` | Holds are an unconditional veto |
| Priya Raghunathan | Orphan — only in `vector-index` | Recall against a derived store whose source is gone |
| Tobias Ferreira | Only in `compliance-archive` | Crypto-shred is the only available mechanism |
| Yuki Abramson | Injection payload in the `profile-store` bio | The tool isn't in the agent's surface, and policy denies anyway |
| Nneka Lindqvist | `notify-suppression` returns `PARTIAL` | Residual honesty — silent partial success is unrepresentable |
| Callum Oyelaran | A batch job recreates the record at T+7 | Resurrection detection |

---

## Architecture

```
┌─ Human ──────────── requester · approver, via a Cognito-authenticated HTTP API
│
├─ Reasoning ──────── Discovery subgraph on AgentCore Runtime
│    └─ the ONLY compute with bedrock:InvokeModel. Read-only tools. Scale-to-zero.
│
├─ Policy boundary ── AgentCore Gateway (MCP) + AgentCore Policy (Cedar, default-deny)
│    └─ per-identity tool filtering: the agent never SEES a mutating tool
│
├─ Control ────────── saga-executor Lambda · LangGraph StateGraph
│    ├─ DynamoDB checkpointer ── the system of record. The pause lives here.
│    ├─ EventBridge Scheduler ── wakes paused threads (grace window, T+7/T+30)
│    └─ no bedrock permission ── invariant 2, enforced by IAM
│
└─ Participants ───── 8 Lambda targets over 8 real AWS services, one 5-verb contract
```

**Determinism on the outside, autonomy on the inside.** The StateGraph is the saga; the discovery subgraph is the one bounded stage where a model runs. Executor nodes are plain Python functions, so replay of an approved manifest never re-enters the model — and now cannot, because the role forbids it.

Full spec: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · Decisions: [`docs/adr/`](docs/adr/) · Diagrams: [`docs/diagrams/`](docs/diagrams/) · Review log: [`docs/VALIDATION.md`](docs/VALIDATION.md) · Build order: [`docs/ROADMAP.md`](docs/ROADMAP.md)

### The Deletion Participant Contract

Every subsystem implements exactly five tools, surfaced through one AgentCore Gateway endpoint. This is the whole extensibility model.

```
subject.discover      read-only. What exists for this subject here?
subject.soft_delete   reversible. Disable, tombstone, mark pending.
subject.restore       the compensating transaction for soft_delete.
subject.hard_delete   irreversible. Purge or crypto-shred.
subject.verify        read-only assertion. Must return zero.
```

Adding participant #9 means writing one Lambda, registering one Gateway target, and passing `make conformance`. The agent's tool surface stays **O(1) in participant count** — which is the point, because a tool surface that grows with N degrades tool-selection accuracy, and that attacks the one metric that must not move.

### Three properties worth stealing even if you use none of this code

**Approval binds to a plan digest, not to a subject.** Otherwise: the approver reviews a three-system plan, the agent re-discovers, the plan grows to include the production database, and execution proceeds under the old approval. The token is bound to `sha256(canonical(manifest))`; any change invalidates it.

**Policy is enforced outside the model's reasoning — and outside the model's *view*.** Guardrails in a system prompt are advisory, and discovery reads subject-controlled content by design. AgentCore Policy evaluates Cedar at the Gateway before any tool runs, and `PartiallyAuthorizeActions` filters the tool list per identity, so the discovery agent is never offered `hard_delete` in the first place. `make eval-adversarial` passes on *"the tool was absent, or policy denied and logged"* — never on *"the model resisted."*

**Recall is the SLO; precision is a convenience.** A false positive costs a reviewer thirty seconds. A false negative is caught by nobody. `make eval` fails the build below recall 1.0, and there is no principled threshold beneath it for a legal obligation.

---

## Quick start

Requires Python 3.10+, an AWS account, credentials with permission to create the stack, and Bedrock model access enabled in your region.

```bash
git clone https://github.com/YOUR-ORG/agentic-pii-erasure
cd agentic-pii-erasure

make install        # venv + dependencies + .env
make check          # lint, unit, policy, cdk synth — no AWS account needed

make deploy-dev     # ⚠️ creates real infrastructure and costs real money
make seed           # write fabricated subjects into the deployed services
make walkthrough    # the full arc, end to end
make destroy-dev    # tear it down
```

> **Building along?** This repo is docs-first and built milestone by milestone with Claude Code — [`docs/ROADMAP.md`](docs/ROADMAP.md) is the build order. Every milestone has two gates: a **hermetic** one that runs in `make check` with no AWS account, and a **deployed** one a human runs after `make deploy-dev`.

Then try the interesting paths:

```bash
make discover SUBJECT=sub_b21c        # blocked by a litigation hold
make discover SUBJECT=sub_c15e        # injection payload — watch the tool list, then the deny
make inspect P=compliance-archive     # the WORM store that has no delete API
make threads                          # paused sagas — checkpoint rows with nothing running
make approve THREAD=... DECISION=approve
make ledger                           # hash-chained audit trail, chain verified
```

The moment worth watching: at the approval gate the executor Lambda **returns**. `make threads` shows a live saga with zero running compute anywhere in the account. That is the property [ADR-016](docs/adr/ADR-016-serverless-durability.md) is built on.

### Everything else

```bash
make check           # what CI runs on every commit: lint, unit, policy, synth  [no AWS]
make conformance     # all 5 verbs x all 8 participants                        [needs AWS]
make integration     # full saga, all three phases                             [needs AWS]
make eval            # discovery recall gate — fails below 1.0                 [needs AWS]
make eval-adversarial
make chaos           # participant failures, duplicate wakes, resurrection
make upgrade-canary  # REQUIRED before any langgraph / checkpoint-aws bump (ADR-016)
make synth           # CDK synth with IAM assertions
make diagrams        # render Mermaid to SVG
make help
```

`make upgrade-canary` is not optional hygiene. It pauses a saga, bumps both pinned packages, and asserts a clean resume. Without it, a serialization change strands live erasure requests silently, past a statutory deadline.

---

## What this costs, and what it buys

There is no free tier for this design, and the honest trade is worth stating in the README rather than a footnote.

| | |
|---|---|
| **Gone** | Hermetic CI for conformance, integration, and the recall gate. Those now need an AWS account, credentials, and money. [ADR-012](docs/adr/ADR-012-simulated-participants.md) was right that a merge gate shouldn't depend on a cloud service being reachable — [ADR-020](docs/adr/ADR-020-deployed-eval-gate.md) records what we do about it. |
| **Still free** | `make check` — lint, unit tests, canonicalisation stability, reducer concurrency, the policy engine, and `cdk synth` with its IAM assertions. The fast loop stays fast. |
| **Bought** | Archetypes that behave like the services they model. Delete markers, GSI lag, Object Lock, KMS deletion windows, SES suppression retention — all real, all catchable. |

**Nothing in the stack bills continuously for existing rather than for working.** That is a deliberate constraint, and enforcing it cost the derived-index participant its original service: it was OpenSearch Serverless, whose OCU floor is charged for as long as the collection exists and which dominated the bill by an order of magnitude over everything else combined. It is now **S3 Vectors** — storage per GB-month plus per-request, no provisioned capacity. **The swap was made purely on cost** ([ADR-021](docs/adr/ADR-021-s3-vectors-for-cost.md)).

An idle stack now costs cents rather than hundreds of dollars a month. That matters beyond the money: for a repo with no local mode, a component that punishes deployment is a structural problem, and CI cost now scales with work done rather than with how long the stack existed.

`make destroy-dev` still matters, but the reason is correctness rather than price — an S3 Object Lock bucket in COMPLIANCE mode cannot be emptied until its retention period expires, by anyone, including root. [`infra/README.md`](infra/README.md) leads with it.

## Known limits

- **Identity resolution is out of scope.** Matching a request to a subject across systems with no shared key is its own project. Subjects arrive pre-resolved.
- **Crypto-shredding's legal status is unsettled.** Some supervisory authorities accept cryptographic erasure; others treat it as pseudonymisation. [ADR-007](docs/adr/ADR-007-crypto-shredding-for-worm.md) records the position and makes legal sign-off a release gate. It does not resolve the question.
- **The grace window and the statutory deadline conflict.** A 30-day window inside a one-month GDPR deadline leaves no margin. See `docs/ARCHITECTURE.md` §16 Q4 — unresolved.
- **The saga inherits Lambda's 15-minute ceiling.** Realistic manifests complete in seconds; a 200-participant tenant would not. §16 Q5 keeps the fork open rather than pretending the ceiling doesn't exist.
- **S3 Vectors is not a low-latency serving store.** Infrequent queries return in under a second; frequently-queried indexes settle around 100 ms. That is fine for discovery and the T+7/T+30 sweeps, which is all this repo does with it. A production system needing single-digit-millisecond search would tier S3 Vectors behind OpenSearch — which reintroduces the OCU floor for the hot tier. [ADR-021](docs/adr/ADR-021-s3-vectors-for-cost.md) is a decision about *this* workload, not a claim that S3 Vectors replaces OpenSearch generally.
- **Checkpoint compatibility is an unsolved operational constraint, not a solved problem.** With a 30-day grace window, in-flight state spans framework versions at all times. Both durability packages are pinned exactly and `make upgrade-canary` gates every bump — but [ADR-016](docs/adr/ADR-016-serverless-durability.md)'s controls reduce the risk rather than removing it. If you would rather AWS owned this, [ADR-003](docs/adr/ADR-003-step-functions-owns-durability.md) documents the Step Functions path it replaced; that fork is still defensible.
- **This is a reference implementation, not a compliance product.** It has not been assessed by anyone's counsel, including its authors'.

## Contributing a participant

1. `cp -r src/pii_erasure/participants/upload_bucket src/pii_erasure/participants/your_system`
2. Implement the five verbs against your service. Return `residual` honestly — a participant that cannot fully delete must say so.
3. Add the service and its Lambda to `infra/stacks/participants.py`, and register the Gateway target in `infra/stacks/gateway.py`.
4. Add seed data in `seeds/` and let the generator emit ground truth for it.
5. `make check` then `make conformance` must pass. That is the entire registration process.

## License

MIT. See [LICENSE](LICENSE).
