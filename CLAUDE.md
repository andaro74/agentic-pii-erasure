# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A reference implementation of agentic, auditable PII erasure across eight **fictional** subsystems. Everything is fake data on the local filesystem. There is no real user data anywhere in this repo and there must never be.

Stack: LangGraph (orchestration and durability), LangChain 1.0 (agents and middleware), Amazon Bedrock (models), MCP (participants), AWS (Aurora, ECS, EventBridge, DynamoDB, S3). **No Step Functions** — see ADR-014.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before making structural changes, and [`docs/adr/`](docs/adr/) before contradicting one. If a change conflicts with an ADR, **write a superseding ADR rather than silently diverging** — the ADR set is the reason this repo is worth reading. The framework decision has already changed twice (009 → 011 → 013); the superseded ADRs are kept deliberately.

---

## How to build here

The repository is **docs-first by design**: `docs/` describes the finished system; `src/` starts near-empty. **[`docs/ROADMAP.md`](docs/ROADMAP.md) defines the build order** — milestones with executable "done when" commands. `/next-milestone` runs the loop.

Session loop:
1. Take the first unchecked milestone in the roadmap (or the one the human names). Restate its goal and "done when" before writing anything.
2. Before writing framework code, **verify the API against the installed pinned version** — read the installed package source or its version's docs. Remembered signatures are not evidence; the pins are exact for a reason (ADR-014).
3. Implement in small steps. Anything that cannot be made to work fails loudly — no stub that pretends success.
4. Done means: the milestone's "done when" command **and** `make check` both pass, with real output shown.
5. Tick the checkbox and fix any doc drift in the same commit, or write a superseding ADR.

**Never build ahead of the current milestone.** The docs describe the target; building target features early creates untested claims — the defect class the last validation pass caught four times ([docs/VALIDATION.md](docs/VALIDATION.md): a test that couldn't pass, a gate that couldn't gate, a pin protecting the wrong layer).

`make check` is green from commit zero: milestone-gated targets print "⏳ lands at Mx" until their entry file exists, then become mandatory automatically. **Never re-add a guard to silence a failing gate.**

`make deploy` is human-only (denied in `.claude/settings.json`, costs money); `make synth` is fine. Custom commands: `/next-milestone`, `/add-participant`, `/validate`.

---

## Invariants

These are not style preferences. Each one exists because violating it produces a specific, serious failure. If a task appears to require breaking one, stop and say so rather than working around it.

### 0. The framework boundary is an explicit allowlist

`langgraph` and `langchain` may be imported **only** from: `discovery/`, `saga/`, `policy/middleware.py`, `approval/gate.py`, and `scheduler/handler.py`. A unit test enforces this list verbatim.

The point is not purity — interrupts, middleware and resume genuinely need the framework. The point is that `contract/`, `manifest/`, `participants/`, `ledger/` and the policy *engine* stay framework-free, because that is what made two framework migrations (ADR-009 → 011 → 013) touch almost nothing. Widening the allowlist is an architectural decision, not a convenience import.

### 1. The discovery agent never gets a mutating tool

The discovery subgraph in `src/pii_erasure/discovery/` is constructed with `subject.discover` and `subject.verify` **only**. Never add `soft_delete`, `hard_delete`, or `restore` to a discovery agent's tool list, not even temporarily for debugging, not even behind a flag.

Discovery reads subject-controlled content (CRM bio fields, file metadata) and is therefore injection-reachable by design. Its lack of privilege is the entire security claim.

### 2. Deletion tools are called by executor nodes, not by models

`src/pii_erasure/saga/nodes/` executor nodes are **plain deterministic Python**. They replay an approved manifest. They must not construct an agent or a model client, call a model, or branch on model output. Replay of an approved plan never re-enters the model.

### 3. Approval binds to the manifest digest

Any code path that mints, validates, or consumes an approval token must carry `manifest_digest`. Never key an approval on `subject_ref` alone. Never re-plan and then execute under a prior approval — produce a new manifest, and require a new approval.

### 4. Canonicalisation must be byte-stable

`contract/canonical.py` is the most fragile thing here. Sorted keys, normalised numbers, deterministic array ordering, **no timestamps or run IDs inside the digested body**. Semantically identical plans must produce identical digests, or approvals churn and the digest binding becomes unusable.

Any change to canonicalisation is a breaking change. Bump `schemaVersion` and add a fixture.

### 5. Never log raw PII

Log `subject_ref` (a pseudonymous handle), never names, emails, or addresses. This applies to logs, traces, exception messages, ledger entries, and agent memory. `observability/redact.py` provides the scrubber; use it. The seeded fake PII is treated exactly as if it were real — that discipline is part of what the repo demonstrates.

### 6. Phase 3 never compensates

If a `hard_delete` fails, retry it. Do not call `restore`. Do not roll back. Route to the DLQ and stop. A compensating transaction that recreates the subject's data converts a failed erasure into an active breach.

`restore` must be unreachable from any phase 3 code path. There is a test asserting this; do not weaken it.

### 7. Participants report residuals honestly

A participant that cannot fully delete returns `PARTIAL` with a populated `residual`. Never return `APPLIED` when work remains. `pigeon-comms` is the worked example — its suppression list legitimately retains an email hash, and that is disclosed rather than hidden.

### 8. Recall gates the build

`make eval` fails below recall 1.0. When the gate goes red, the fix is a better discovery agent or a new fixture — **never a lowered threshold**. See ADR-008.

### 9. Never widen the `langgraph` version constraint

`pyproject.toml` pins an exact version. This is not over-caution. A saga pauses for 30 days at the approval gate; if the framework is upgraded mid-window, resume must deserialize a checkpoint written by the old version. A serialization change strands live erasure requests **silently**, past a statutory deadline.

Any bump requires `make upgrade-canary` to pass — pause a saga, upgrade, assert clean resume. See ADR-014.

### 10. Reducers are a correctness surface, not a detail

`saga/state.py` reducers decide how concurrent node writes merge. Get one wrong and two participants' discovery results silently overwrite each other. That surfaces as a **recall failure**, not a crash — the exact failure mode ADR-008 exists to prevent.

Every reducer needs a unit test with concurrent writes. Default to append/merge semantics; never last-write-wins on a collection.

### 11. Resume handlers must be idempotent

EventBridge Scheduler can deliver more than once. A duplicate resume of a phase 3 node is a duplicate deletion attempt. `scheduler/handler.py` must be idempotent per `(thread_id, wake_reason)` and must not rely on participant idempotency keys alone as the only defence.

---

## Layout

```
src/pii_erasure/
  contract/       5-verb schemas, canonicalisation, idempotency keys
  manifest/       Pydantic models, digest, signing, validation
  participants/   8 fake subsystems, each an MCP server over JSON state
    _base/        shared harness — inherit, don't copy the verb plumbing
  discovery/      LangGraph subgraph. Read-only tools only. (Invariant 1)
  saga/           LangGraph StateGraph — the system of record
    state.py      TypedDict + reducers. (Invariant 10)
    nodes/        deterministic functions. No model client. (Invariant 2)
    checkpointer.py  SQLite local / Aurora Postgres prod
  scheduler/      EventBridge Scheduler + resume Lambda. (Invariant 11)
  policy/         LangChain middleware, Cedar engine, optional Gateway client
  approval/       interrupt()/Command(resume=…), token minting, digest binding
  ledger/         hash-chained append-only audit log
  observability/  structlog + OTel setup, PII redaction
  cli/            typer entrypoints (seed, discover, demo, inspect, ledger, threads, resume)
```

Docs live in `docs/`, CDK in `infra/`, Cedar policies in `policies/cedar/`, fake data in `seeds/`, evals in `evals/`.

## Conventions

- **Python 3.10+.** Type hints everywhere; `mypy --strict` must pass on `src/`.
- **Pydantic v2** for every boundary object. Contract types live in `contract/`, never redefined locally.
- **`structlog`** for logging, never bare `print` outside `cli/`.
- **Line length 100**, ruff for lint and format. `make fmt` before committing.
- Participants inherit from `participants/_base`. If you find yourself copying verb plumbing, extend the base instead.
- Tests mirror `src/` layout. Conformance tests are parameterised over the participant registry, so a new participant is automatically covered — do not write bespoke conformance tests per participant.

## Commands

```bash
make install        # venv + dev extras
make seed           # populate fake subsystems
make demo-offline   # full walkthrough, stub model, no AWS
make check          # lint + unit + conformance + policy  (run before every commit)
make conformance    # 5 verbs x 8 participants
make integration    # full three-phase saga
make eval           # recall gate
```

`make check` is the gate. Run it before declaring work complete.

## LangGraph specifics

- `StateGraph`, `interrupt`, `Command` from `langgraph`; agents and middleware from `langchain`; `ChatBedrockConverse` from `langchain_aws`; MCP participants via `langchain-mcp-adapters`.
- **Verify API surface against the pinned version's docs before writing code.** This ecosystem moves; do not trust remembered signatures.
- The saga is compiled with a checkpointer. The checkpointer is the system of record — not a cache, not an optimisation. Nothing may hold saga state outside it.
- The approval gate calls `interrupt()` inside a node and resumes via `Command(resume=…)`. The process is expected to *exit* while paused; do not hold a thread waiting for a human.
- Policy is two-layered: LangChain middleware wraps every tool call as a fast in-process pre-check, and AgentCore Gateway with Cedar is the authoritative boundary in production. Keep the decision logic in `policy/engine.py` so both backends evaluate identical rules.
- Discovery is a **subgraph**, not a free-running agent, so its tool list is fixed at construction and assertable.
- Offline mode (`PII_ERASURE_OFFLINE=1`) swaps in a stub model and the SQLite checkpointer so CI is hermetic and free. New code must work under the stub; if it cannot, extend the stub.

## AWS specifics

- Bedrock is the model provider. No other provider is configured; adding one is fine, defaulting to one is not.
- Aurora PostgreSQL Serverless v2 holds checkpoints in production. Availability of the erasure pipeline is coupled to it — treat it as tier-1.
- EventBridge Scheduler fires one-shot schedules at a resume Lambda. Timers are **ours** now; Step Functions is gone.
- CDK lives in `infra/` and is opt-in. `make demo` must never require a deployed stack.

## Things not to do

- Do not add a generic "run this query" or "call this API" participant tool. It voids the policy layer — Cedar cannot express a constraint over arbitrary SQL, and blast radius becomes unbounded.
- Do not store saga state anywhere but the checkpointer. A second source of truth reintroduces exactly the divergence problem ADR-014 removed.
- Do not reintroduce Step Functions piecemeal. If the timer burden proves too high, the answer is ADR-014's named alternative (LangGraph Platform) or a documented reversal — not a hybrid nobody decided on.
- Do not make the demo call real AWS services by default. Offline and free is the point.
- Do not add real PII, real customer names, or real company names to seeds or tests.
- Do not weaken a failing gate to make CI green.
- Do not "improve" the architecture doc's honest caveats (unsettled crypto-shred legality, the grace-window conflict) into confident claims. Marking uncertainty is deliberate.
