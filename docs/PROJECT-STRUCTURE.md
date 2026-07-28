# Project structure

Annotated layout for `agentic-pii-erasure`. Companion to [ARCHITECTURE.md](ARCHITECTURE.md), which explains *why*; this file explains *where*.

Stack: **Amazon Bedrock AgentCore** (Runtime, Gateway, Policy, Identity, Memory, Observability) · **LangGraph + LangChain 1.0** (graphs, agents, middleware) · **AWS Lambda** (the saga) · **DynamoDB** (checkpoints, ledger, registries) · **EventBridge Scheduler** (timers) · **KMS**, **S3 Object Lock**, **S3 Vectors**, **Cognito**, **Aurora Serverless v2**, **Glue/Athena**, **SES** (the participants).

**Deploys to AWS only.** There is no local mode — see [ADR-017](adr/ADR-017-real-aws-participants.md).

## Top level

```
agentic-pii-erasure/
├── README.md                  front door — problem, idea, deploy
├── CLAUDE.md                  invariants and conventions for Claude Code
├── LICENSE                    MIT
├── pyproject.toml             ⚠️ langgraph AND langgraph-checkpoint-aws are PINNED. See ADR-016.
├── requirements.lock          the transitive layer under those pins (make lock; invariant 9)
├── Makefile                   the only interface anyone needs
├── .env.example               region, stack name, model ID — no secrets, SigV4 only
├── .github/workflows/ci.yml   hermetic gates · ephemeral eval stack · upgrade canary
├── .claude/                   Claude Code settings + /next-milestone /add-participant /validate
├── docs/ROADMAP.md            the build order — milestones with executable "done when" gates
├── docs/                      architecture, ADRs, diagrams
├── infra/                     ⚠️ CDK — the deployment IS the product. Not optional.
├── src/pii_erasure/           the platform
├── seeds/                     made-up subject data, written into real AWS services
├── policies/cedar/            the deployed AgentCore Policy artifact
├── evals/                     ground truth, evaluators, adversarial corpus
├── tests/                     unit · conformance · integration · upgrade canary
└── scripts/                   one-off developer utilities
```

## `src/pii_erasure/`

### `contract/` — the five-verb contract

Depends on nothing. Everything else depends on it. **Framework- and cloud-independent** — this package survived two framework changes and one cloud-native rewrite untouched, which is the clearest evidence [ADR-001](adr/ADR-001-agent-proposes-saga-disposes.md)'s boundary sits in the right place.

```
contract/
├── verbs.py          DiscoverRequest/Response, SoftDeleteRequest/Response, …
├── archetypes.py     Archetype enum: AUTHORITATIVE_IDENTITY, WORM, DERIVED_INDEX, …
├── outcomes.py       Outcome: APPLIED | ALREADY_APPLIED | REFUSED | PARTIAL
│                  Deletability: NOT_PRESENT | DELETABLE | PARTIAL | BLOCKED_BY_HOLD
├── canonical.py      ⚠️ byte-stable canonical JSON. Fragile. CLAUDE.md invariant 4.
│                  A documented subset of RFC 8785 — ADR-022.
├── idempotency.py    sha256(sagaId ‖ systemId ‖ operation ‖ canonical(artifacts))
└── registry.py       participant registry; conformance tests parameterise over this
```

`idempotency.py` carries more weight in a serverless design than it did in a server-based one. Lambda retries, EventBridge Scheduler at-least-once delivery, and checkpoint resume after a crash all replay participant calls. Idempotency is load-bearing for correctness, not just for retries.

### `manifest/` — the artifact the agent produces

```
manifest/
├── models.py         Pydantic v2 DeletionManifest, Participant, LegalHold, ResidualRisk
├── digest.py         sha256 over canonical form; excludes provenance entirely
├── signing.py        KMS asymmetric sign/verify (ECC_NIST_P256 / ECDSA_SHA_256)
└── validate.py       schema version compatibility, completeness assertions
```

### `participants/` — eight real AWS services, eight Lambda handlers

Each package is one Lambda function, registered as an AgentCore Gateway target. The Gateway turns the declared schema into MCP tools; the agent never learns there are eight backends.

```
participants/
├── _base/
│   ├── handler.py           Lambda entrypoint harness — dispatches the 5 verbs
│   ├── idempotency.py       DynamoDB applied-key log; returns ALREADY_APPLIED on replay
│   ├── guard.py             re-validates manifestDigest + approvalToken in-process
│   └── holds.py             legal hold evaluation shared across participants
├── cognito_identity/        AUTHORITATIVE_IDENTITY · Cognito · revoke first, delete last
├── profile_store/           DOCUMENT_STORE      · DynamoDB · GSI fan-out and lag
├── billing_ledger/          RELATIONAL          · Aurora Serverless v2 via RDS Data API
├── upload_bucket/           BLOB                · S3 versioning · delete marker ≠ deletion
├── compliance_archive/      WORM                · Object Lock + KMS · DEK shred only
├── vector_index/            DERIVED_INDEX       · S3 Vectors · orphan embeddings. ADR-021.
├── analytics_lake/          COLUMNAR            · Glue/Athena Iceberg · rewrite or shred
└── notify_suppression/      RESIDUAL_BY_DESIGN  · SES · the suppression entry must stay
```

`_base/guard.py` is defence in depth, not redundancy: AgentCore Policy is the control, and the participant is the backstop for a misconfigured Gateway target. A participant that receives a `hard_delete` without a valid digest-bound token refuses, logs, and returns `REFUSED`.

Aurora is reached through the **RDS Data API** specifically so that `billing_ledger` needs no VPC attachment. Nothing in this repo attaches a Lambda to a VPC. A VPC does exist, because Aurora cannot exist without one — see [ADR-023](adr/ADR-023-aurora-needs-a-vpc.md).

`vector_index/` carries one constraint worth knowing before you read it: S3 Vectors has **no delete-by-query**. `DeleteVectors` takes keys (≤500 per call), so vector keys are derived deterministically from `subjectRef` and never stored in a side mapping table that could be lost independently of the vectors it addresses. It replaced an OpenSearch Serverless participant purely to remove that service's continuous OCU floor ([ADR-021](adr/ADR-021-s3-vectors-for-cost.md)).

### `discovery/` and `runtime/` — the one place a model runs

```
discovery/
├── subgraph.py       builds the discovery subgraph; read-only tool list asserted at construction
├── agents/
│   ├── cartographer.py    enumerate candidate systems (Resource Explorer, tags, Config)
│   ├── prospector.py      probe candidates for subject-shaped keys
│   ├── lineage.py         follow derived-store dependencies (Glue catalog, discover responses)
│   ├── counsel.py         legal holds and Art. 17(3) exemptions — holds veto
│   └── editor.py          reconcile findings into one candidate manifest
├── advisor.py        the ONE model. Emits scope hints only — additive, key-shaped,
│                     bounded, and degrading to silence on any failure
├── memory.py         AgentCore Memory priors — topology only, pre-write scrubber. ADR-019.
└── tools.py          MCP client over the AgentCore Gateway endpoint

runtime/
├── entrypoint.py     AgentCore Runtime HTTP contract (/invocations, /ping)
└── (no Dockerfile)   ADR-025: ships as an S3 code zip, arm64 wheels, no ECR.
                      `cdk synth` runs in `make check`, and a DockerImageAsset
                      would build at synth time — a Docker daemon inside the
                      hermetic gate. entrypoint.py serves the contract on the
                      stdlib, so a unit test can start it in-process.
```

Invariant 1 is enforced in three places, each independently sufficient: the tool list asserted read-only in `subgraph.py` at construction with a unit test behind it; the Cedar permit that names only `discover` and `verify`; and Gateway tool-list filtering via `PartiallyAuthorizeActions`, which means the model is never *offered* a mutating tool. Discovery output is a candidate manifest — it mutates nothing, so a discovery failure is always fail-closed.

### `saga/` — the StateGraph, in Lambda

The system of record. This is where [ADR-016](adr/ADR-016-serverless-durability.md) lives.

```
saga/
├── handler.py          ⚠️ Lambda entrypoint — drives the graph to the next interrupt or END;
│                       refuses a resume shaped for a different gate before it reaches the
│                       graph, because a delivered value is a persisted one (V9-3)
├── graph.py            StateGraph assembly, compile(checkpointer=…)
├── state.py            TypedDict state schema + reducers      ← correctness surface
├── edges.py            conditional routing between phases — declarative path maps
├── checkpointer.py     DynamoDBSaver (langgraph-checkpoint-aws) + S3 offload
├── deps.py             the seam: every AWS client the nodes use, injectable for tests
├── invoker.py          direct Lambda invocation of participants — the M5→M6 seam;
│                       Cedar-gated Gateway routing replaces it for mutations at M6
├── nodes/              ⚠️ deterministic functions — no model client. Invariant 2.
│   ├── _shared.py          manifest/digest access, the cross-node verify pass
│   ├── intake.py
│   ├── hold_check.py
│   ├── plan.py             M5: validates+signs the provided fixture manifest;
│   │                       M7: invokes the Runtime when none is provided
│   ├── soft_delete.py      phase 2 · backward recovery
│   ├── approval_gate.py    interrupt() — the Lambda RETURNS here, for days
│   ├── grace_window.py     schedules the wake, then interrupts
│   ├── hold_recheck.py     re-evaluated at phase 3 entry, never cached from phase 1
│   ├── hard_delete.py      phase 3 · forward only · ONE participant per superstep,
│   │                       so each receipt checkpoints individually; stuck = DLQ + pause
│   ├── verify.py
│   └── sweep.py            T+7 / T+30 resurrection detection
├── compensate.py       phase 2 only. Unreachable from phase 3 — asserted by test.
├── ordering.py         derived→authoritative, shred last (ranks position the
│                       irreversible action, §7.1); revoke-first is structural
└── tombstone.py        registry blocking re-creation; consulted by all write paths
```

`nodes/plan.py` is the only node that touches the reasoning plane, and it does so by **invoking the Runtime and receiving a manifest** — not by holding a model client. The `saga-executor` execution role has no `bedrock:*` permission, so invariant 2 is enforced by IAM as well as by the import test (invariant 12).

`state.py` deserves attention. Reducers decide how concurrent node writes merge — get a reducer wrong and two participants' results silently overwrite each other, which shows up as a **recall failure**, not as a crash. Every reducer carries a concurrent-write unit test, and those tests are hermetic.

`ordering.py` encodes the constraint that surprises people: **delete derived stores before authoritative ones**, because the authoritative record is the join key.

### `scheduler/` — wall-clock timers

The piece Step Functions used to provide free. [ADR-016](adr/ADR-016-serverless-durability.md) names this as an ongoing cost of owning durability.

```
scheduler/
├── base.py           Scheduler protocol: schedule_resume(thread_id, at, reason)
├── eventbridge.py    EventBridge Scheduler one-shot schedules
└── handler.py        resume Lambda: load checkpoint, Command(resume=…)
```

Exactly-once resume is the hazard. `handler.py` must be idempotent per `(thread_id, wake_reason)` — EventBridge Scheduler delivers at least once, and a duplicate resume of a phase 3 node is a duplicate deletion attempt. Participant idempotency keys catch it downstream; the handler must not rely on that alone (invariant 11).

### `policy/` — enforcement outside the model

```
policy/
├── engine.py         evaluates THE DEPLOYED .cedar files in-process — the fast pre-check
├── schema.py         reconstructs the Cedar schema from the tool manifest (ADR-018)
├── decisions.py      structured allow/deny logging; feeds the adversarial eval
└── middleware.py     LangChain middleware around every tool call        [lands at M7]
```

Two layers by design, and only one of them is the control. **AgentCore Policy at the Gateway is authoritative** ([ADR-018](adr/ADR-018-agentcore-policy.md)); `engine.py` is a fast in-process pre-check and a test surface, because in-process enforcement is bypassable by any caller that forgets it.

**There is no divergence test, because there is no second rule set.** Earlier plans had `engine.py` reimplementing "a declared Cedar subset" in Python, with a test to stop the two drifting. `engine.py` instead evaluates the *same* `policies/cedar/*.cedar` the stack deploys, through the same Cedar implementation — drift removed rather than policed ([ADR-024](adr/ADR-024-cedar-expresses-identity-and-shape.md)).

**`gateway.py` and `context.py` do not exist, deliberately.** There is no `AuthorizeAction` call to make: AgentCore Policy evaluates *inside* the Gateway on every tool invocation, and filters `tools/list` per identity server-side — there is no client-side API in the SDK to wrap. And `context.py` would have built a decision context of `subjectCount`/`holdCount`/`digest`, none of which Cedar can read; the only context is the tool's own `context.input`. Writing either would have produced a module that looks like a control and calls nothing.

### `approval/`, `ledger/`, `observability/`, `cli/`

```
approval/
├── gate.py           interrupt() payload construction; Command(resume=…) handling
├── tokens.py         mints tokens bound to sha256(canonical(manifest))
├── presenter.py      blast radius, baseline diff, residual risk, irreversibility clock
└── api.py            Lambda behind the Cognito-authenticated HTTP API

ledger/
├── chain.py          each entry carries its predecessor's digest
├── writer.py         append-only DynamoDB writes; Streams → Firehose → S3 Object Lock
└── verify.py         recompute and compare — `make ledger` runs this

observability/
├── logging.py        structlog config
├── tracing.py        OTel → CloudWatch; trace id == thread id == sagaId
└── redact.py         PII scrubber. Used everywhere, including every Memory write. Invariant 5.

cli/
├── main.py           typer app
└── commands/         seed · discover · walkthrough · inspect · ledger · threads · resume · approve
```

`threads` and `resume` are operator tools, not conveniences. A paused saga is a checkpoint row in DynamoDB with no running compute anywhere; operators need to list and resume them without the AWS console.

## `infra/` — CDK, and not optional

The deployment *is* the product ([ADR-017](adr/ADR-017-real-aws-participants.md)). `cdk synth` runs in `make check` on every commit and carries security assertions that are cheaper to enforce here than at runtime.

```
infra/
├── app.py
├── README.md              ⚠️ cost warning FIRST, then teardown, then deploy
└── stacks/
    ├── naming.py          AgentCore's TWO name conventions. Gateway/Target take
    │                      `asdp-dev-…`; Policy/PolicyEngine/AgentRuntime take
    │                      `asdp_dev_…`, ≤48 chars. Refuses at synth (V10-1)
    ├── foundation.py      KMS CMK · DynamoDB tables (checkpoints, ledger, tombstones,
    │                      DEK registry, idempotency) · S3 buckets · EventBridge bus
    ├── participants.py    the 8 real services + their Lambda handlers, one role each
    ├── gateway.py         AgentCore Gateway · targets · Policy attachment · Identity
    ├── runtime.py         AgentCore Runtime for discovery · S3 code zip (ADR-025) ·
    │                      Memory store · a role with NO participant IAM
    ├── saga.py            saga-executor + resume Lambdas · Scheduler role · SQS DLQ
    ├── api.py             HTTP API + Cognito authorizer for intake, approval, operator reads
    └── observability.py   alarms and dashboards for ARCHITECTURE §10.1
```

Assertions that live in `cdk synth` because a runtime test would be too late:

- the `saga-executor` role has **no `bedrock:*`** action (invariant 12)
- the DEK registry table has `pointInTimeRecovery` **disabled** and no AWS Backup selection (invariant 14, threat T9)
- no Lambda in the stack has a VPC configuration
- the ledger archive bucket has Object Lock in COMPLIANCE mode
- the discovery Runtime role has no participant-service actions
- every AgentCore Policy and PolicyEngine `Name` matches the pattern the **installed
  service model** declares — synth validates the template, not the service, so without
  this the rejection arrives from CloudFormation mid-deploy (V10-1)
- every `Description` in every stack satisfies IAM's `roleDescriptionType` — the
  narrowest of the per-service constraints, and the one that rejects the em dashes this
  repo's prose uses everywhere else (V10-2)

## `evals/`, `seeds/`, `tests/`

```
evals/
├── run.py                `python -m evals.run --suite discovery --fail-under-recall 1.0`
├── fixtures/generator.py  writes synthetic subjects into the REAL services AND emits
│                          ground truth in the same pass — ADR-020
├── evaluators/           recall (hard fail < 1.0) · precision · holds · trajectory ·
│                         residual_honesty · no_pii_in_memory · tool_surface_minimality
└── adversarial/corpus.json

tests/
├── unit/           contract, canonicalisation, digest binding, policies, reducers,
│                   participant handler logic (moto), synth/IAM assertions   [hermetic]
├── fixtures/
│   └── canonical/  golden canonical bytes + sha256 per input, carrying the
│                   canonicalisation schemaVersion. Changing a rule turns these
│                   red rather than silently re-digesting outstanding approvals.
├── conformance/    parameterised over the registry — 5 verbs × 8 participants  [needs AWS]
│                   Seeds its own throwaway subject per case; participants whose
│                   milestone has not landed skip with a reason and become
│                   mandatory automatically when their function appears.
└── integration/    full saga; chaos; compensation; resurrection; upgrade canary [needs AWS]
```

`moto` covers participant handler *logic* — argument shaping, ordering, residual construction — so the fast loop stays fast. It is never a substitute for a gate: the interesting failures are delete markers, GSI lag, Object Lock, and KMS deletion windows, and a mock reproduces none of them.

Chaos and durability cases worth keeping green:

- participant fails in phase 2 → **full compensation**, subject restored everywhere
- participant fails in phase 3 → **no compensation attempted**, SQS DLQ raised, saga halts
- **executor killed mid-phase** → re-invoke, resume from checkpoint, no duplicate participant calls
- **upgrade canary** → pause, bump both pinned packages, resume cleanly *(release gate, ADR-016)*
- EventBridge Scheduler fires twice → exactly one resume
- manifest mutated after approval → Gateway policy denial and security alarm
- hold appears during the grace window → phase 3 refuses at re-check
- subject reappears at T+7 → resurrection incident, distinct from a deletion failure

## Dependency direction

```
contract  ←  manifest  ←  saga  ←  cli
    ↑           ↑        ↗   ↖
participants  policy  discovery  scheduler
                ↑        ↑
             gateway  runtime → discovery
                         ↑
                     approval → saga
```

`contract/` depends on nothing. Framework imports are confined to an **explicit allowlist** — `discovery/`, `runtime/`, `saga/`, `policy/middleware.py`, `approval/gate.py`, `scheduler/handler.py` — enforced by a unit test that names the list verbatim. Everything else (`contract/`, `manifest/`, `participants/`, `ledger/`, the policy *engine*) is framework-free, which is what kept the framework migrations cheap and what would keep a fourth one cheap.

Cloud SDK imports are not boundaried the same way — this is an AWS-only platform and pretending otherwise would be theatre. But `contract/` and `manifest/models.py` stay `boto3`-free, because they are the two files a reader should be able to lift wholesale.
