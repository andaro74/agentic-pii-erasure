# Agentic PII Erasure

**Automated, auditable, multi-system deletion of a data subject — where the agent never deletes anything.**

A production-shaped reference implementation of [GDPR Art. 17](https://gdpr-info.eu/art-17-gdpr/) erasure across eight fictional subsystems, built with [LangGraph](https://langchain-ai.github.io/langgraph/), LangChain 1.0, Amazon Bedrock, MCP, and a three-phase saga.

Runs entirely on your laptop against fake data. No AWS account required.

```bash
make install && make seed && make demo-offline
```

---

## The problem

Deleting a user is presented as a CRUD operation. It is not. It is a **distributed transaction across systems that will never agree to a two-phase commit**, executed against a participant set that **is not known at design time**, with a **legally mandated completeness guarantee** and **no undo**.

Four properties make it genuinely hard:

1. **Unknown participants.** Nobody has an accurate map of which systems hold data for a given subject. CMDBs are stale. Lineage tooling covers the warehouse and not the seventeen services writing into it.
2. **No compensating transaction.** The saga pattern assumes every forward action has an inverse. `DELETE` does not.
3. **Physically undeletable stores.** WORM buckets and append-only logs cannot service a row-level delete. Deletion has to be redefined as *irreversible loss of readability*.
4. **Completeness is binary.** Deleting 7 of 8 systems is not 87% success. It is a reportable breach with a clean audit trail saying otherwise.

## The idea

> **The agent proposes. The saga disposes.**

Non-determinism is an asset in exactly one place — **discovery**, where the search space is open-ended. It is a liability everywhere else.

The model never calls a deletion tool. It produces a **signed, versioned Deletion Manifest**. Execution is deterministic replay of that manifest, gated by policy the model cannot reach. When a regulator asks *"why was this record deleted?"*, the answer is a signed artifact and an approver's identity — not "the model decided."

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

## The eight fictional subsystems

Meridian Outfitters is a made-up multi-brand retailer. Each subsystem is a real MCP server over fake data, and each one is hard in a different way.

| Participant | Archetype | The lesson it teaches | Real-world analogue |
|---|---|---|---|
| `atlas-identity` | Authoritative identity | Revoke first — stop new writes before deleting old ones | Okta, Cognito |
| `helios-crm` | Document store | Nested documents and secondary indexes fan out | MongoDB, DynamoDB |
| `ledger-billing` | Relational | FK ordering; statutory financial retention beats erasure | Postgres, Aurora |
| `vault-files` | Blob store | Versioning means a delete marker is **not** a deletion | S3, GCS |
| `aegis-archive` | **WORM** | No delete API exists. Crypto-shred or nothing. | S3 Object Lock |
| `beacon-search` | Derived index | Orphan documents outlive their deleted source | OpenSearch, Elastic |
| `quarry-lake` | Columnar analytics | You cannot delete a row from a Parquet file | Athena, Snowflake |
| `pigeon-comms` | Third-party SaaS | The suppression list **must** retain a hash — residual by design | Mailchimp, SES |

`aegis-archive` and `pigeon-comms` are the two worth reading first. Neither can honour a deletion request in the way the word implies, and pretending otherwise is how compliance incidents happen.

## The seven made-up subjects

Each seeded subject exercises a different path. They are the demo *and* the eval fixtures.

| Subject | Scenario | What it proves |
|---|---|---|
| Marisol Okonkwo | Present in 6 of 8, no holds | Happy path, end to end |
| Dmitri Vasquez-Lund | Litigation hold in `ledger-billing` | Holds are an unconditional veto |
| Priya Raghunathan | Orphan — only in `beacon-search` | Recall against a derived store whose source is gone |
| Tobias Ferreira | Only in `aegis-archive` | Crypto-shred is the only available mechanism |
| Yuki Abramson | Injection payload in the CRM bio field | Policy denies where the model may not |
| Nneka Lindqvist | `pigeon-comms` returns `PARTIAL` | Residual honesty — silent partial success is unrepresentable |
| Callum Oyelaran | A batch job recreates the record at T+7 | Resurrection detection |

---

## Architecture

```
┌─ Human ──────────────── requester · approver
│
├─ Saga (LangGraph StateGraph) ── deterministic nodes, conditional edges
│    ├─ Discovery subgraph ────── the one place a model runs. Read-only.
│    └─ interrupt() ───────────── pauses days-to-weeks at the approval gate
│
├─ Checkpointer ──────────────── SQLite locally · Aurora PostgreSQL in prod
├─ EventBridge Scheduler ─────── wakes paused threads (grace window, T+7/T+30)
├─ Policy (LangChain middleware + AgentCore Gateway/Cedar) ── default-deny
│
└─ Participants (MCP) ────────── 8 subsystems, one uniform 5-verb contract
```

**Determinism on the outside, autonomy on the inside.** The StateGraph is the saga; the discovery subgraph is the one bounded stage where a model runs. Executor nodes are plain Python functions, so replay of an approved manifest never re-enters the model.

**There is no Step Functions.** LangGraph checkpointers are the system of record — one orchestrator instead of two, and the saga is unit-testable in Python. [ADR-014](docs/adr/ADR-014-langgraph-owns-durability.md) records what that costs: you build the wall-clock timers, and you own checkpoint compatibility across a 30-day pause.

Full spec: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · Decisions: [`docs/adr/`](docs/adr/) · Diagrams: [`docs/diagrams/`](docs/diagrams/) · Review log: [`docs/VALIDATION.md`](docs/VALIDATION.md) · Build order: [`docs/ROADMAP.md`](docs/ROADMAP.md)

### The Deletion Participant Contract

Every subsystem implements exactly five MCP tools. This is the whole extensibility model.

```
subject.discover      read-only. What exists for this subject here?
subject.soft_delete   reversible. Disable, tombstone, mark pending.
subject.restore       the compensating transaction for soft_delete.
subject.hard_delete   irreversible. Purge or crypto-shred.
subject.verify        read-only assertion. Must return zero.
```

Adding subsystem #9 means writing one MCP server and passing `make conformance`. The agent's tool surface stays **O(1) in participant count** — which is the point, because a tool surface that grows with N degrades tool-selection accuracy, and that attacks the one metric that must not move.

### Three properties worth stealing even if you use none of this code

**Approval binds to a plan digest, not to a subject.** Otherwise: the approver reviews a three-system plan, the agent re-discovers, the plan grows to include the production database, and execution proceeds under the old approval. The token is bound to `sha256(canonical(manifest))`; any change invalidates it.

**Policy is enforced outside the model's reasoning.** Guardrails in a system prompt are advisory, and discovery reads subject-controlled content by design. LangChain middleware wraps every tool call and is default-deny, with AgentCore Gateway and Cedar as the authoritative boundary in production. `make eval-adversarial` passes on *"policy denied and logged"*, never on *"the model resisted."*

**Recall is the SLO; precision is a convenience.** A false positive costs a reviewer thirty seconds. A false negative is caught by nobody. `make eval` fails the build below recall 1.0, and there is no principled threshold beneath it for a legal obligation.

---

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/YOUR-ORG/agentic-pii-erasure
cd agentic-pii-erasure

make install        # venv + dependencies + .env
make seed           # populate the 8 fake subsystems
make demo-offline   # full walkthrough, stub model, zero cost
```

> **Building along?** This repo is docs-first and built milestone by milestone with Claude Code — [`docs/ROADMAP.md`](docs/ROADMAP.md) is the build order. CI is green from commit zero: milestone-gated stages print "⏳ lands at Mx" until they exist, then become mandatory automatically.

`make demo-offline` uses a deterministic stub model, so it needs no AWS account and produces identical output every run. For real agentic discovery, configure Bedrock credentials in `.env` and use `make demo`.

Then try the interesting paths:

```bash
make discover SUBJECT=sub_b21c   # blocked by a litigation hold
make discover SUBJECT=sub_c15e   # injection payload — watch the policy deny
make inspect P=aegis-archive     # the WORM store that has no delete API
make threads                     # paused sagas sitting in checkpoints
make resume THREAD=... DECISION=approve
make ledger                      # hash-chained audit trail, chain verified
```

Kill the process mid-saga and re-run — it resumes from the checkpoint. That is the property ADR-014 is built on, and it is worth seeing once.

### Everything else

```bash
make check           # what CI runs: lint, unit, conformance, policy
make conformance     # all 5 verbs x all 8 participants
make integration     # full saga, all three phases
make eval            # discovery recall gate — fails below 1.0
make eval-adversarial
make upgrade-canary  # REQUIRED before any langgraph bump (ADR-014)
make synth           # CDK synth for the AWS deployment
make diagrams        # render Mermaid to SVG
make help
```

`make upgrade-canary` is not optional hygiene. It pauses a saga, bumps the framework, and asserts a clean resume. Without it, a serialization change strands live erasure requests silently, past a statutory deadline.

---

## What is deliberately simulated

Honesty about scope matters more than an impressive feature list.

| Simulated here | Production equivalent |
|---|---|
| In-process MCP servers over JSON files | Real subsystems behind AgentCore Gateway |
| LangChain middleware policy only | Middleware **plus** AgentCore Policy (Cedar) at the Gateway boundary |
| SQLite checkpointer | `langgraph-checkpoint-postgres` on Aurora Serverless v2 |
| In-process asyncio timers | EventBridge Scheduler → resume Lambda |
| Graph runs in your shell | ECS Fargate service |
| Grace window in seconds | 30 days, statutory |
| Hash-chained JSONL ledger | DynamoDB + Streams → S3 Object Lock COMPLIANCE |
| Local key registry for crypto-shred | KMS envelope keys, registry with PITR **disabled** |

The `policies/cedar/*.cedar` files are the real production artifact. The local engine implements a declared subset of their semantics so the demo runs offline; install the `cedar` extra to validate them against a real Cedar engine.

## Known limits

- **Identity resolution is out of scope.** Matching a request to a subject across systems with no shared key is its own project. Subjects arrive pre-resolved.
- **Crypto-shredding's legal status is unsettled.** Some supervisory authorities accept cryptographic erasure; others treat it as pseudonymisation. [ADR-007](docs/adr/ADR-007-crypto-shredding-for-worm.md) records the position and makes legal sign-off a release gate. It does not resolve the question.
- **The grace window and the statutory deadline conflict.** A 30-day window inside a one-month GDPR deadline leaves no margin. See `docs/ARCHITECTURE.md` §16.4 — unresolved.
- **Checkpoint compatibility is an unsolved operational constraint, not a solved problem.** With a 30-day grace window, in-flight state spans multiple framework versions at all times. `langgraph` is pinned to an exact version and `make upgrade-canary` gates every bump — but the controls in [ADR-014](docs/adr/ADR-014-langgraph-owns-durability.md) reduce the risk rather than removing it. If you would rather AWS owned this, [ADR-003](docs/adr/ADR-003-step-functions-owns-durability.md) documents the Step Functions path it replaced; that fork is still defensible.
- **This is a reference implementation, not a compliance product.** It has not been assessed by anyone's counsel, including its authors'.

## Deploying to AWS

Optional and not on the critical path. `make synth` renders the CDK; `infra/README.md` carries the cost warning and teardown. The stack is Aurora Serverless v2 (checkpoints), ECS Fargate (the graph), EventBridge Scheduler plus a resume Lambda (timers), DynamoDB and S3 Object Lock (ledger), and optionally AgentCore Gateway (the policy boundary).

Aurora dominates the bill. Tear down when you are done.

## Contributing a participant

1. `cp -r src/pii_erasure/participants/vault_files src/pii_erasure/participants/your_system`
2. Implement the five verbs. Return `residual` honestly — a participant that cannot fully delete must say so.
3. Add seed data in `seeds/` and ground truth in `evals/fixtures/`.
4. `make conformance` must pass. That is the entire registration process.

## License

MIT. See [LICENSE](LICENSE).
