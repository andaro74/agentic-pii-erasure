# Project structure

Annotated layout for `agentic-pii-erasure`. Companion to [ARCHITECTURE.md](ARCHITECTURE.md), which explains *why*; this file explains *where*.

Stack: **LangGraph** (orchestration and durability), **LangChain 1.0** (agents and middleware), **Amazon Bedrock** (models), **MCP** (participants), **AWS** (Aurora, ECS, EventBridge, DynamoDB, S3). No Step Functions — see [ADR-014](adr/ADR-014-langgraph-owns-durability.md).

## Top level

```
agentic-pii-erasure/
├── README.md                  front door — problem, idea, quick start
├── CLAUDE.md                  invariants and conventions for Claude Code
├── LICENSE                    MIT
├── pyproject.toml             ⚠️ langgraph is PINNED, not ranged. See ADR-014.
├── Makefile                   the only interface anyone needs
├── .env.example               PII_ERASURE_OFFLINE=1 needs no AWS account
├── .github/workflows/ci.yml   quality · conformance · saga · upgrade canary · recall gate
├── .claude/                   Claude Code settings + /next-milestone /add-participant /validate
├── docs/ROADMAP.md            the build order — milestones with executable "done when" gates
├── docs/                      architecture, ADRs, diagrams
├── infra/                     CDK — Aurora, Fargate, EventBridge Scheduler, Gateway
├── src/pii_erasure/           the platform
├── seeds/                     made-up data for the 8 fictional subsystems
├── policies/cedar/            production Cedar policies
├── evals/                     ground truth, evaluators, adversarial corpus
├── tests/                     unit · conformance · integration · upgrade canary
└── scripts/                   one-off developer utilities
```

## `src/pii_erasure/`

### `contract/` — the five-verb contract

Depends on nothing. Everything else depends on it. **Framework-independent** — this package survived two framework changes untouched, which is the clearest evidence ADR-001's boundary sits in the right place.

```
contract/
├── verbs.py          DiscoverRequest/Response, SoftDeleteRequest/Response, …
├── archetypes.py     Archetype enum: AUTHORITATIVE_IDENTITY, WORM, DERIVED_INDEX, …
├── outcomes.py       Outcome enum: APPLIED | ALREADY_APPLIED | REFUSED | PARTIAL
├── canonical.py      ⚠️ byte-stable canonical JSON. Fragile. CLAUDE.md invariant 4.
├── idempotency.py    sha256(sagaId ‖ systemId ‖ operation ‖ canonical(artifacts))
└── registry.py       participant registry; conformance tests parameterise over this
```

`idempotency.py` carries more weight than it used to. Under ADR-014 a saga can resume from a checkpoint after a crash, so participant calls **will** be replayed. Idempotency is now load-bearing for correctness, not just for retries.

### `manifest/` — the artifact the agent produces

```
manifest/
├── models.py         Pydantic v2 DeletionManifest, Participant, LegalHold, ResidualRisk
├── digest.py         sha256 over canonical form; excludes timestamps and run IDs
├── signing.py        local: Ed25519. production: KMS asymmetric sign.
└── validate.py       schema version compatibility, completeness assertions
```

### `participants/` — the eight fictional subsystems

Real MCP servers over JSON state. Surfaced to the graph through `langchain-mcp-adapters`.

```
participants/
├── _base/
│   ├── server.py       MCP server harness — registers the 5 verbs
│   ├── store.py        JSON-backed state with an applied-idempotency-key log
│   └── holds.py        legal hold evaluation shared across participants
├── atlas_identity/     AUTHORITATIVE_IDENTITY · revoke first, delete last
├── helios_crm/         DOCUMENT_STORE     · nested docs, secondary index fan-out
├── ledger_billing/     RELATIONAL         · FK ordering; statutory retention holds
├── vault_files/        BLOB               · versioning; delete marker ≠ deletion
├── aegis_archive/      WORM               · no delete API — crypto-shred only
├── beacon_search/      DERIVED_INDEX      · orphan docs outlive their source
├── quarry_lake/        COLUMNAR           · cannot delete a row from a Parquet file
└── pigeon_comms/       THIRD_PARTY_SAAS   · suppression list retains a hash by design
```

### `discovery/` — the one place a model runs

```
discovery/
├── subgraph.py       builds the discovery subgraph; read-only tool list asserted at construction
├── agents/
│   ├── cartographer.py    enumerate candidate systems
│   ├── prospector.py      probe candidates for subject-shaped keys
│   ├── lineage.py         follow derived-store dependencies
│   ├── counsel.py         legal holds and Art. 17(3) exemptions — holds veto
│   └── editor.py          reconcile findings into one manifest
└── stub_model.py     deterministic model for PII_ERASURE_OFFLINE=1 and CI
```

The tool list is asserted read-only in `subgraph.py` at construction time, with a unit test behind the assertion. CLAUDE.md invariant 1 is enforced in code, not by convention. Discovery output is a candidate manifest — it mutates nothing, so a discovery failure is always fail-closed.

### `saga/` — the LangGraph StateGraph

The system of record. This is where ADR-014 lives.

```
saga/
├── graph.py            StateGraph assembly, compile(checkpointer=…)
├── state.py            TypedDict state schema + reducers      ← LangGraph-specific
├── edges.py            conditional routing between phases
├── checkpointer.py     SQLite (local) / Postgres on Aurora (prod)
├── nodes/              ⚠️ deterministic functions — no model client. Invariant 2.
│   ├── intake.py
│   ├── hold_check.py
│   ├── plan.py             manifest synthesis + signature
│   ├── soft_delete.py      phase 2 · backward recovery
│   ├── approval_gate.py    interrupt() — pauses here for days
│   ├── grace_window.py     schedules the wake, then interrupts
│   ├── hold_recheck.py     re-evaluated at phase 3 entry, never cached from phase 1
│   ├── hard_delete.py      phase 3 · forward recovery only
│   ├── verify.py
│   └── sweep.py            T+7 / T+30 resurrection detection
├── compensate.py       phase 2 only. Unreachable from phase 3 — asserted by test.
├── ordering.py         derived→authoritative, children→parents, shred last
└── tombstone.py        registry blocking re-creation; consulted by all write paths
```

`state.py` is new under LangGraph and deserves attention. Reducers decide how concurrent node writes merge — get a reducer wrong and two participants' results silently overwrite each other, which shows up as a **recall failure**, not as a crash.

`ordering.py` encodes the constraint that surprises people: **delete derived stores before authoritative ones**, because the authoritative record is the join key.

### `scheduler/` — wall-clock timers

The piece Step Functions used to provide free. ADR-014 names this the largest single cost of dropping it.

```
scheduler/
├── base.py           Scheduler protocol: schedule_resume(thread_id, at, reason)
├── local.py          in-process asyncio timers — dev and CI only
├── eventbridge.py    EventBridge Scheduler one-shot schedules → resume Lambda
└── handler.py        Lambda entrypoint: load checkpointer, Command(resume=…)
```

Exactly-once resume is the hazard. `handler.py` must be idempotent per `(thread_id, wake_reason)` — EventBridge can deliver more than once, and a duplicate resume of a phase 3 node is a duplicate deletion attempt. Idempotency keys catch it; the handler should not rely on that alone.

### `policy/` — enforcement outside the model

```
policy/
├── middleware.py     LangChain middleware, wrap-style around every tool call
├── engine.py         evaluates a declared Cedar subset; same rules as policies/cedar/
├── gateway.py        optional AgentCore Gateway client — the authoritative boundary
├── context.py        builds the decision context (subjectCount, holdCount, digest, …)
└── decisions.py      structured allow/deny logging; feeds the adversarial eval
```

Two layers by design. Middleware is a fast in-process pre-check; **AgentCore Gateway with Cedar is the authoritative boundary**, because ADR-005 holds that in-process enforcement is bypassable if any caller forgets it. Local runs use middleware only, and the README says so.

### `approval/`, `ledger/`, `observability/`, `cli/`

```
approval/
├── gate.py           interrupt() payload construction; Command(resume=…) handling
├── tokens.py         mints tokens bound to sha256(canonical(manifest))
└── presenter.py      blast radius, baseline diff, residual risk, irreversibility clock

ledger/
├── chain.py          each entry carries its predecessor's digest
├── writer.py         append-only; DynamoDB in prod, JSONL locally
└── verify.py         recompute and compare — `make ledger` runs this

observability/
├── logging.py        structlog config
├── tracing.py        OTel → CloudWatch; trace id == thread id so traces join
└── redact.py         PII scrubber. Used everywhere. Invariant 5.

cli/
├── main.py           typer app
└── commands/         seed · discover · demo · inspect · ledger · threads · resume
```

`threads` and `resume` are new. Under ADR-014 a paused saga is a checkpoint thread, and operators need to list and resume them without the AWS console.

## `infra/` — CDK

```
infra/
├── app.py
├── README.md              ⚠️ cost warning and teardown instructions
└── stacks/
    ├── checkpointer.py    Aurora PostgreSQL Serverless v2 + Secrets Manager
    ├── compute.py         ECS Fargate service running the graph
    ├── scheduler.py       EventBridge Scheduler role + resume Lambda + API Gateway
    ├── gateway.py         AgentCore Gateway + Cedar policy attachment
    └── ledger.py          DynamoDB + Streams → Firehose → S3 Object Lock
```

Deployment is opt-in. `make demo` runs entirely locally and touches none of this.

## `evals/`, `seeds/`, `tests/`

```
evals/
├── run.py                `python -m evals.run --suite discovery --fail-under-recall 1.0`
├── fixtures/generator.py  writes synthetic subjects AND emits ground truth in one pass
├── evaluators/           recall (hard fail < 1.0) · precision · holds · trajectory ·
│                         residual_honesty · no_pii_in_memory
└── adversarial/corpus.json

tests/
├── unit/           contract, canonicalisation stability, digest binding, policies, reducers
├── conformance/    parameterised over the registry — 5 verbs × 8 participants
└── integration/    full saga; chaos; compensation; resurrection; upgrade canary
```

Chaos and durability cases worth keeping green:

- participant fails in phase 2 → **full compensation**, subject restored everywhere
- participant fails in phase 3 → **no compensation attempted**, DLQ raised, saga halts
- **process killed mid-phase** → resume from checkpoint, no duplicate participant calls
- **upgrade canary** → pause, bump `langgraph`, resume cleanly *(release gate, ADR-014)*
- timer fires twice → exactly one resume
- manifest mutated after approval → policy denial and security alarm
- hold appears during the grace window → phase 3 refuses at re-check

## Dependency direction

```
contract  ←  manifest  ←  saga  ←  cli
    ↑           ↑        ↗   ↖
participants  policy  discovery  scheduler
                         ↑
                     approval → saga
```

`contract/` depends on nothing. Framework imports are confined to an **explicit allowlist** — `discovery/`, `saga/`, `policy/middleware.py`, `approval/gate.py`, `scheduler/handler.py` — enforced by a unit test that names the list verbatim. Everything else (`contract/`, `manifest/`, `participants/`, `ledger/`, the policy *engine*) is framework-free, which is what keeps the framework decision cheap to reverse a third time.
