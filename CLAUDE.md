# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A reference implementation of agentic, auditable PII erasure across eight **real AWS services**, deployed serverlessly and running on Amazon Bedrock. All subject data is fabricated — there is no real user data in this repo and there must never be — but it is written into real Cognito user pools, real DynamoDB tables, and a real S3 Object Lock bucket, and it is treated exactly as if it were real.

**This platform deploys to AWS. There is no local mode.** No stub model, no SQLite checkpointer, no in-process participants, no `demo-offline`. `make deploy-dev` is the entry point. See [ADR-017](docs/adr/ADR-017-real-aws-participants.md).

Stack: **Amazon Bedrock AgentCore** (Runtime, Gateway, Policy, Identity, Memory, Observability) · **LangGraph + LangChain 1.0** (graphs, interrupts, the Bedrock model client — no agents, no middleware: ADR-026) · **Lambda** (the saga) · **DynamoDB** (checkpoints, ledger, registries) · **EventBridge Scheduler** (timers) · **KMS**, **S3 Object Lock**, **S3 Vectors**, **Cognito**, **Aurora Serverless v2**, **Glue/Athena**, **SES** (the participants). **No Step Functions, no Fargate, and nothing we run attached to a VPC** — see ADRs 015–017 and [ADR-023](docs/adr/ADR-023-aurora-needs-a-vpc.md), which records why Aurora forces a VPC to *exist* and why no Lambda joins it.

**Almost nothing in the stack bills continuously for existing rather than for working**, and the exception is named rather than glossed: one Secrets Manager secret, $0.40/month, which the RDS Data API requires and which is what keeps every Lambda out of a VPC (V13-4). That is a hard constraint, not a nice-to-have: it is why the derived-index participant is S3 Vectors and not OpenSearch Serverless ([ADR-021](docs/adr/ADR-021-s3-vectors-for-cost.md)). Before adding any AWS service, check whether it has a provisioned floor — if it does, it needs an ADR arguing why the floor is worth it. **`tests/unit/test_cost_floors.py` now applies that rule to the synthesised templates**, so it is a build failure rather than something to remember at review time.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before making structural changes, and [`docs/adr/`](docs/adr/) before contradicting one. If a change conflicts with an ADR, **write a superseding ADR rather than silently diverging** — the ADR set is the reason this repo is worth reading. Three decisions have already changed on the record: framework (009 → 011 → 013), durability (003 → 014 → 016), and participants (012 → 017). The superseded ADRs are kept deliberately.

---

## How to build here

The repository is **docs-first by design**: `docs/` describes the finished system; `src/` starts near-empty. **[`docs/ROADMAP.md`](docs/ROADMAP.md) defines the build order** — milestones with executable "done when" commands. `/next-milestone` runs the loop.

Session loop:
1. Take the first unchecked milestone in the roadmap (or the one the human names). Restate its goal and both "done when" gates before writing anything.
2. Before writing framework code, **verify the API against the installed pinned version** — read the installed package source or its version's docs. Before writing AWS code, **verify the API shape against the service's current documentation**, not memory. AgentCore is young and moves; remembered signatures are not evidence, and the pins are exact for a reason ([ADR-016](docs/adr/ADR-016-serverless-durability.md)).
3. Implement in small steps. Anything that cannot be made to work fails loudly — no stub that pretends success.
4. Done means: the milestone's **hermetic** gate and `make check` both pass, with real output shown.
5. The milestone's **deployed** gate is the human's to run. Say plainly which gate you ran and which you did not.
6. Tick the checkbox and fix any doc drift in the same commit, or write a superseding ADR.

**Never build ahead of the current milestone.** The docs describe the target; building target features early creates untested claims — the defect class the last validation pass caught four times ([docs/VALIDATION.md](docs/VALIDATION.md): a test that couldn't pass, a gate that couldn't gate, a pin protecting the wrong layer, a fixture that couldn't fail).

`make check` is green from commit zero **and needs no AWS account**: lint, unit tests, policy tests, and `cdk synth` with its IAM assertions. Milestone-gated targets print "⏳ lands at Mx" until their entry file exists, then become mandatory automatically. **Never re-add a guard to silence a failing gate.**

**Anything that spends money or mutates real infrastructure is human-only** and denied in `.claude/settings.json`: `make bootstrap`, `make deploy`, `make deploy-dev`, `make destroy-dev`, and any raw `cdk bootstrap`/`deploy`/`destroy`. `make synth` and `make preflight` are fine — preflight is read-only and free, and asks AWS the questions the hermetic gate cannot (V8-5). Custom commands: `/next-milestone`, `/add-participant`, `/validate`.

---

## Invariants

These are not style preferences. Each one exists because violating it produces a specific, serious failure. If a task appears to require breaking one, stop and say so rather than working around it.

### 0. The framework boundary is an explicit allowlist

`langgraph` and `langchain` may be imported **only** from: `discovery/`, `runtime/`, `saga/`, `approval/gate.py`, and `scheduler/handler.py`. A unit test enforces this list verbatim.

`policy/middleware.py` was on this list until [ADR-026](docs/adr/ADR-026-no-middleware-seam.md) removed it. LangChain middleware intercepts an *agent's* tool calls; the one model here holds no tools, so there was never a seam to attach to and the file was never built. The list narrows — it does not widen to accommodate a file nobody wrote.

The point is not purity — interrupts and resume genuinely need the framework. The point is that `contract/`, `manifest/`, `participants/`, `ledger/` and the policy *engine* stay framework-free, because that is what made two framework migrations (ADR-009 → 011 → 013) touch almost nothing. Widening the allowlist is an architectural decision, not a convenience import.

`boto3` is not boundaried the same way — this is an AWS-only platform and pretending otherwise would be theatre. But `contract/` and `manifest/models.py` stay `boto3`-free, because they are the two files a reader should be able to lift wholesale.

### 1. The discovery agent never gets a mutating tool

The discovery subgraph in `src/pii_erasure/discovery/` is constructed with `subject.discover` and `subject.verify` **only**. Never add `soft_delete`, `hard_delete`, or `restore` to a discovery agent's tool list, not even temporarily for debugging, not even behind a flag.

This is enforced in three independent places: the tool list asserted at subgraph construction, the Cedar permit that names only the two read verbs, and AgentCore Gateway tool-list filtering via `PartiallyAuthorizeActions` — which means the model is never *offered* a mutating tool. Do not weaken any of the three on the grounds that the other two exist.

Discovery reads subject-controlled content (profile bio fields, S3 object metadata) and is therefore injection-reachable by design. Its lack of privilege is the entire security claim.

### 2. Deletion tools are called by executor nodes, not by models

`src/pii_erasure/saga/nodes/` executor nodes are **plain deterministic Python**. They replay an approved manifest. They must not construct an agent or a model client, call a model, or branch on model output. Replay of an approved plan never re-enters the model.

`nodes/plan.py` is the single exception to "the saga talks to the reasoning plane," and it does so by *invoking the Runtime and receiving a manifest* — never by holding a model client.

### 3. Approval binds to the manifest digest

Any code path that mints, validates, or consumes an approval token must carry `manifest_digest`. Never key an approval on `subject_ref` alone. Never re-plan and then execute under a prior approval — produce a new manifest, and require a new approval.

### 4. Canonicalisation must be byte-stable

`contract/canonical.py` is the most fragile thing here. Sorted keys, normalised numbers, deterministic array ordering, **no timestamps, run IDs, trace IDs, or AgentCore Runtime session IDs inside the digested body**. Semantically identical plans must produce identical digests, or approvals churn and the digest binding becomes unusable.

Any change to canonicalisation is a breaking change. Bump `schemaVersion` and add a fixture.

### 5. Never log raw PII

Log `subject_ref` (a pseudonymous handle), never names, emails, or addresses. This applies to logs, CloudWatch traces, exception messages, ledger entries, AgentCore Observability spans, and AgentCore Memory. `observability/redact.py` provides the scrubber; use it. The seeded fake PII is treated exactly as if it were real — that discipline is part of what the repo demonstrates.

### 6. Phase 3 never compensates

If a `hard_delete` fails, retry it. Do not call `restore`. Do not roll back. Route to the SQS DLQ and stop. A compensating transaction that recreates the subject's data converts a failed erasure into an active breach.

`restore` must be unreachable from any phase 3 code path. There is a test asserting this; do not weaken it.

### 7. Participants report residuals honestly

A participant that cannot fully delete returns `PARTIAL` with a populated `residual`. Never return `APPLIED` when work remains. `notify-suppression` is the worked example — the SES suppression list legitimately retains an email hash, and that is disclosed rather than hidden. `analytics-lake` is the second: Iceberg rows survive until snapshot expiry.

### 8. Recall gates the build

`make eval` fails below recall 1.0. When the gate goes red, the fix is a better discovery agent or a new fixture — **never a lowered threshold**. See [ADR-008](docs/adr/ADR-008-recall-1.0-hard-gate.md) and [ADR-020](docs/adr/ADR-020-deployed-eval-gate.md).

Ground truth is **generated, not labelled**: the fixture generator writes into the real services and emits the placement map in the same pass. Never hand-write a ground-truth map, and never derive one from the agent's output.

### 9. Never widen the durability version constraints

`pyproject.toml` pins **`langgraph` and `langgraph-checkpoint-aws`** to exact versions. This is not over-caution. A saga pauses for 30 days at the approval gate; if the framework is upgraded mid-window, resume must deserialize a checkpoint written by the old version. A serialization change strands live erasure requests **silently**, past a statutory deadline.

Serialization lives in the checkpoint package as much as in `langgraph`, so the two move in lockstep. Any bump requires `make upgrade-canary` to pass — pause a saga, upgrade, assert clean resume. See [ADR-016](docs/adr/ADR-016-serverless-durability.md).

**Two packages, three places.** The pins are written in `pyproject.toml` (what the tests run against), `requirements.lock` (generated from it), and the Makefile's `SAGA_PINS` (what `make package` installs into the saga Lambda). Conflating "both pins" the packages with "both pins" the locations is what made the canary unable to pass at all (V12-6): `tests/unit/test_upgrade_canary_contract.py` now derives the file list from the tree, so a fourth location fails `make check` rather than a release.

### 10. Reducers are a correctness surface, not a detail

`saga/state.py` reducers decide how concurrent node writes merge. Get one wrong and two participants' discovery results silently overwrite each other. That surfaces as a **recall failure**, not a crash — the exact failure mode ADR-008 exists to prevent.

Every reducer needs a unit test with concurrent writes. Default to append/merge semantics; never last-write-wins on a collection.

### 11. Resume handlers must be idempotent

EventBridge Scheduler delivers at least once. A duplicate resume of a phase 3 node is a duplicate deletion attempt. `scheduler/handler.py` must be idempotent per `(thread_id, wake_reason)` and must not rely on participant idempotency keys alone as the only defence.

### 12. The saga has no Bedrock permission

The `saga-executor` and `resume` Lambda execution roles must never be granted `bedrock:*`, and their only AgentCore permission is the single `plan` node's Runtime invocation. Invariant 2 used to be a code-review rule backed by an import test; it is now also an IAM denial, asserted in `cdk synth`.

If a task seems to need the saga to call a model, the answer is that it needs a new manifest from the reasoning plane — not a widened role.

### 13. AgentCore Memory holds topology, never subject data

Memory is a **cross-subject** surface: something learned deleting subject A is retrieved while deleting subject B. Writing anything subject-shaped there leaks across the exact boundary this architecture protects (threat T7).

Permitted: which systems a tenant holds data in, derived-store relationships, productive scope hints. Forbidden: `subjectRef`, artifact locators, counts, classifications, hold IDs, approver identities, manifest digests — **no per-subject facts at all**.

The pre-write scrubber **rejects** a suspect write rather than sanitising it, and `no_pii_in_memory` fails the build. Priors are advisory: a prior may reorder discovery, never cause a system to be skipped. See [ADR-019](docs/adr/ADR-019-agentcore-memory-priors.md).

### 14. The DEK registry is never backed up

The DynamoDB table holding per-subject wrapped data keys must have point-in-time recovery **disabled** and must appear in no AWS Backup plan, no cross-region replica, and no export. A restore of that table un-shreds every subject deleted since the restore point — it converts completed erasures into an active breach (threat T9).

Asserted by unit test and by a `cdk synth` assertion. Do not enable PITR "for safety." Its absence *is* the safety property.

---

## Layout

```
src/pii_erasure/
  contract/       5-verb schemas, canonicalisation, idempotency keys
  manifest/       Pydantic models, digest, KMS signing, validation
  participants/   8 real AWS services, each a Lambda behind an AgentCore Gateway target
    _base/        shared harness — inherit, don't copy the verb plumbing
  discovery/      LangGraph subgraph. Read-only tools only. (Invariant 1)
  runtime/        AgentCore Runtime entrypoint (/invocations + /ping). S3 code zip,
                  not a container — ADR-025
  saga/           LangGraph StateGraph in Lambda — the system of record
    state.py      TypedDict + reducers. (Invariant 10)
    nodes/        deterministic functions. No model client. (Invariant 2)
    handler.py    Lambda entrypoint — drives the graph to the next interrupt or END
    checkpointer.py  DynamoDBSaver + S3 offload
  scheduler/      EventBridge Scheduler + resume Lambda. (Invariant 11)
  policy/         in-process engine + schema + decisions — the divergence surface for
                  `make policy-test`. No middleware: ADR-026
  approval/       interrupt()/Command(resume=…), token minting, digest binding, HTTP API
  ledger/         hash-chained append-only audit log → S3 Object Lock
  observability/  structlog + OTel setup, PII redaction
  cli/            typer entrypoints (seed, discover, walkthrough, inspect, ledger, threads, resume, approve)
```

Docs live in `docs/`, CDK in `infra/`, Cedar policies in `policies/cedar/`, fabricated seed data in `seeds/`, evals in `evals/`.

## Conventions

- **Python 3.10+.** Type hints everywhere; `mypy --strict` must pass on `src/`.
- **Pydantic v2** for every boundary object. Contract types live in `contract/`, never redefined locally.
- **`structlog`** for logging, never bare `print` outside `cli/`.
- **Line length 100**, ruff for lint and format. `make fmt` before committing.
- Participants inherit from `participants/_base`. If you find yourself copying verb plumbing, extend the base instead.
- Conformance tests are parameterised over the participant registry, so a new participant is automatically covered — do not write bespoke conformance tests per participant.
- **`moto` is for unit-testing handler logic only.** It is never a gate. The failures that matter — delete markers, GSI lag, Object Lock, KMS deletion windows, **foreign keys** — are precisely the ones it does not model. The hand-written fakes are held to the same standard: when a deployed run shows a service refusing something a fake accepted, teach the fake that one rule (V12-3). A fake that cannot fail the way the service fails is not testing the handler, it is agreeing with it.
- **No Lambda gets a VPC configuration.** Aurora is reached via the RDS Data API for exactly this reason. A `cdk synth` assertion enforces it.

## Commands

```bash
make install        # venv + dev extras
make check          # lint + unit + policy + synth  — hermetic, no AWS account, run before every commit
make synth          # CDK synth with IAM assertions
make preflight      # region-specific checks synth cannot make (engine versions). Read-only, free

# these cost money and are human-only (denied in .claude/settings.json)
make bootstrap      # once per account + region — creates the CDK toolkit stack
make deploy-dev     # deploy the dev stack. Reads AWS_REGION from .env; fails if unset
make seed           # populate the deployed participants with fabricated subjects
make conformance    # 5 verbs x 8 participants, against the deployed stack
make integration    # full three-phase saga
make eval           # recall gate — fails below 1.0
make walkthrough    # the full arc, end to end
make destroy-dev    # tear it down. Do this.
```

`make check` is the gate a session must leave green. `make deploy-dev` and everything downstream of it is the human's.

## AgentCore specifics

- **Runtime** hosts the discovery subgraph from an **S3 code zip** (arm64, `PYTHON_3_13`) with the `/invocations` + `/ping` HTTP contract — not a container ([ADR-025](docs/adr/ADR-025-runtime-ships-a-code-zip.md)), because `cdk synth` runs inside `make check` and a `DockerImageAsset` would put a Docker daemon in the hermetic gate. Sessions cap at 8 hours async, 15 minutes sync; each gets an isolated microVM. It is the **only** compute permitted to call Bedrock.
- **Gateway** is the single MCP endpoint. Participants are Lambda targets; the agent never learns there are eight backends. The tool surface stays O(1) in participant count, which protects tool-selection accuracy — and that protects recall.
- **Policy** evaluates Cedar on every tool call via `AuthorizeAction`, and filters the tool list per identity via `PartiallyAuthorizeActions`. Deny-by-default, forbid-wins. `LOG_ONLY` → `ENFORCING` is a stack parameter, so flipping it lands in CloudTrail.
- **Identity** provides the workload identities that are the Cedar principals: `asdp-discovery`, `asdp-saga-executor`, `asdp-approval-service`. Distinct from the IAM roles backing them — both layers are required.
- **Memory** holds topology priors only (invariant 13).
- **Observability** captures agent spans natively; the Lambda planes export OTel to the same destination. `thread_id` == `sagaId` == trace correlation key.
- **Cedar entity and context names are validated against the Gateway's generated schema at deploy time, never assumed.** A policy referencing a context key the Gateway does not inject is a policy that silently never fires — decoration, not a control.

## LangGraph specifics

- `StateGraph`, `interrupt`, `Command` from `langgraph`; `ChatBedrockConverse` from `langchain_aws`; the checkpointer is `DynamoDBSaver` from `langgraph-checkpoint-aws`. **No `create_agent` and no middleware** — the discovery subgraph's nodes call the toolset directly and the model holds no tools ([ADR-026](docs/adr/ADR-026-no-middleware-seam.md)).
- **Verify API surface against the pinned version's docs before writing code.** This ecosystem moves; do not trust remembered signatures.
- The saga is compiled with a checkpointer. The checkpointer is the system of record — not a cache, not an optimisation. Nothing may hold saga state outside it.
- The approval gate calls `interrupt()` inside a node and resumes via `Command(resume=…)`. **The Lambda is expected to return while paused.** Do not hold an invocation waiting for a human, and do not treat the pause as a liveness dependency.
- Discovery is a **subgraph**, not a free-running agent, so its tool list is fixed at construction and assertable.

## AWS specifics

- Bedrock is the model provider. No other provider is configured; adding one is fine, defaulting to one is not.
- Everything is serverless and nothing we run attaches to a VPC. Aurora Serverless v2 runs at `min_capacity = 0` ACU and is reached through the RDS Data API. A VPC exists solely to hold the cluster — isolated subnets, no NAT, no endpoints, no continuous cost (ADR-023).
- EventBridge Scheduler fires one-shot schedules at a resume Lambda. Timers are ours; Step Functions is gone.
- **No component may bill continuously for existing.** S3 Vectors replaced OpenSearch Serverless purely to remove its OCU floor ([ADR-021](docs/adr/ADR-021-s3-vectors-for-cost.md)); an idle stack now costs cents. Bedrock tokens are the largest line item on an active one.
- **`make destroy-dev` is still not optional**, but the reason is correctness rather than price: dev stacks use a short Object Lock retention period, because a COMPLIANCE-mode bucket cannot be emptied until retention expires — by anyone, including root. `infra/README.md` leads with it.
- S3 Vectors has **no delete-by-query**. `DeleteVectors` takes keys (≤500 per call), so `vector-index` derives its keys deterministically from `subjectRef`. Never introduce a side mapping table that could be lost independently of the vectors it addresses.

## Things not to do

- Do not add a generic "run this query" or "call this API" participant tool. It voids the policy layer — Cedar cannot express a constraint over arbitrary SQL, and blast radius becomes unbounded.
- Do not store saga state anywhere but the checkpointer. A second source of truth reintroduces exactly the divergence problem ADR-016 removed.
- Do not reintroduce a local mode, a stub model, or an in-process participant. ADR-017 removed them deliberately; a simulation only reproduces the behaviours its author already understood.
- Do not reintroduce Step Functions, Fargate, or a VPC piecemeal. If a constraint proves unworkable, write a superseding ADR — do not drift into a hybrid nobody decided on.
- Do not add real PII, real customer names, or real company names to seeds or tests.
- Do not weaken a failing gate to make CI green, and do not mock a participant to make a deployed gate hermetic.
- Do not "improve" the architecture doc's honest caveats (unsettled crypto-shred legality, the grace-window conflict, the 15-minute saga ceiling, the S3 Vectors latency profile) into confident claims. Marking uncertainty is deliberate — and when a caveat *is* resolved, mark it resolved on the record rather than deleting it, as §16 Q7 shows.
