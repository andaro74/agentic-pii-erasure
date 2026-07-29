# Build roadmap

The repository is **docs-first by design**: `docs/` describes the finished system; `src/` starts near-empty. This file defines the order in which the system gets built. Each milestone is sized for one to a few Claude Code sessions and has an **executable "done when"** — a command, not an opinion.

## Rules of the build

1. **Work the first unchecked milestone** unless the human names another. Tick the box only after both "done when" gates pass.
2. **Never build ahead.** The docs describe the target; building target features early creates untested claims — the exact defect class the validation pass caught four times ([VALIDATION.md](VALIDATION.md)).
3. **Verify APIs against the installed pinned versions** before writing framework code, and against the *deployed* service before writing AWS code. The pins are exact for a reason ([ADR-016](adr/ADR-016-serverless-durability.md)); remembered signatures are not evidence, and neither are remembered API shapes for AgentCore.
4. **`make check` stays green at every commit, with no AWS account.** Unbuilt stages print "⏳ lands at Mx"; when a milestone lands, its gate becomes mandatory automatically. Never re-add a guard to silence a failing gate.
5. **Docs move with code.** Drift found or created gets fixed in the same commit, or a superseding ADR is written.

## Two gates per milestone

There is no local mode ([ADR-017](adr/ADR-017-real-aws-participants.md)), so from M2 onward each milestone has **two** done-when gates:

| Gate | Runs where | Who runs it | Costs money |
|---|---|---|---|
| **Hermetic** | `make check` — lint, unit, policy, `cdk synth` + IAM assertions | Anyone, any commit, no AWS account | No |
| **Deployed** | `make conformance` / `integration` / `eval` against a dev stack | **A human, after `make deploy-dev`** | Yes |

`make deploy-dev`, `make deploy`, and `make destroy-dev` are **human-only** — they are denied in `.claude/settings.json` because they spend money and mutate real infrastructure. A Claude Code session takes a milestone as far as its hermetic gate, then hands the deployed gate to the human and records the real output in the commit.

## When each `make` target lights up

| Target | Milestone | | Target | Milestone |
|---|---|---|---|---|
| `install` `lint` `fmt` `test` `synth` `preflight` | M0 · preflight M4 | | `policy-test` | M6 |
| `conformance` | M2 | | `eval` `eval-adversarial` | M7 |
| `seed` `inspect` | M4 | | `walkthrough` `threads` `resume` | M8 |
| `integration` | M3 (signing) · M5 (saga) | | `upgrade-canary` `chaos` | M9 |
| | | | `deploy` (prod stack) | M10 |

---

## - [x] M0 · Walking skeleton + a stack that synthesises

**Goal:** the package installs, imports, has a CLI, and `infra/` synthesises to CloudFormation. Everything after this is adding organs to a living body — and on an AWS-only platform, "living" includes the deployment artifact.

**Build:** `cli/main.py` (typer app: `--help`, `version`; every unbuilt command prints "⏳ lands at Mx" and exits non-zero — a stub that pretends success violates the repo's ethos) · `observability/logging.py` + `redact.py` skeleton (structlog config, scrubber with tests) · `infra/app.py` + `stacks/foundation.py` (KMS CMK, the DynamoDB tables, S3 buckets, EventBridge bus) · **the committed lockfile** (uv or pip-tools) covering the transitive layer under the invariant-9 pins — until it lands, only the two direct pins are protected · package `__init__.py` files.

**Hermetic done when:** `make install && make check` green · `make synth` emits a template · `erasure --help` shows the app · the lockfile is committed and CI installs from it.

**Traps:** `redact.py` is invariant 5's mechanism, so even the skeleton gets a test proving an email never survives the scrubber · the foundation stack already contains the DEK registry table, so the "PITR disabled, no backup selection" synth assertion lands here, not later (invariant 14) · the lockfile is part of invariant 9's mechanism, not packaging hygiene — VALIDATION baseline finding #3 is what happens when the pin doesn't cover the layer that breaks.

## - [x] M1 · The contract

**Goal:** `contract/` — the package everything depends on and the highest-risk file in the repo. Entirely hermetic; no AWS.

**Build:** `verbs.py`, `archetypes.py`, `outcomes.py`, `registry.py`, `idempotency.py`, and `canonical.py` with a serious test file: shuffled key order → identical bytes; semantically-unordered arrays sorted by defined key; numeric form normalisation; **no timestamps, run IDs, or Runtime session IDs in the digested body**.

**Hermetic done when:** `make test` green with the canonicalisation stability suite · `mypy --strict` clean.

**Traps:** invariant 4. Any later change to canonicalisation is a breaking change requiring a `schemaVersion` bump and a fixture. The rules that landed are a *documented subset* of RFC 8785 — [ADR-022](adr/ADR-022-canonical-json-subset.md). Get the property-style tests in *now* — [ADR-006](adr/ADR-006-approval-binds-to-digest.md)'s digest binding is only as strong as this file.

## - [x] M2 · Participant harness + the two hardest participants + conformance

**Goal:** prove the five-verb contract against two real AWS services — one that lies about deletion, and one that cannot delete at all.

**Build:** `participants/_base/{handler,idempotency,holds}.py` (Lambda handler harness, DynamoDB idempotency log, shared hold evaluation) · `upload_bucket` (S3 versioning: a delete marker is not a deletion; version + delete-marker purge) · `compliance_archive` (Object Lock COMPLIANCE + KMS: `hard_delete` deletes the wrapped DEK from the registry; after shred, decryption failure is distinguishable from not-found) · `infra/stacks/participants.py` for both · `infra/stacks/gateway.py` registering them as AgentCore Gateway targets · `tests/conformance/` **parameterised over the registry** — never bespoke per participant.

**Hermetic done when:** `make check` green — handler logic unit-tested with `moto`, `cdk synth` clean.

**Deployed done when:** `make deploy-dev && make conformance` green for both participants.

**Traps:** invariant 7 (`PARTIAL` + `residual`, never a hopeful `APPLIED`) · conformance asserts `discover` is side-effect-free via a snapshot diff **of the real bucket**, including version and delete-marker state · replayed idempotency key → `ALREADY_APPLIED`, not double-apply · [ADR-007](adr/ADR-007-crypto-shredding-for-worm.md)'s trap: the DEK registry is excluded from every backup path, asserted by test **and** by the synth assertion from M0 · dev stacks use a short Object Lock retention or the bucket cannot be torn down.

## - [x] M3 · Manifest + KMS signing

> **Complete 2026-07-26.** Hermetic half in `6dcb91f` (40 unit tests). Deployed gate run
> by the human; corroborated in CloudTrail by the suite's exact KMS signature — 2 Sign +
> 2 Verify per run, with the tampered-body test producing **no** Verify because the digest
> recomputation rejects it before KMS is asked, which is the order `signing.py` promises.

**Build:** `manifest/{models,digest,signing,validate}.py` — Pydantic v2 models, digest over `canonical()` of the body (**provenance excluded**), KMS asymmetric sign/verify (`ECC_NIST_P256`), immutability after signature.

**Hermetic done when:** unit tests: mutate any field → digest changes; change provenance (session ID, trace ID, timestamps) → digest identical; sign/verify round-trip against a KMS stub; re-plan produces a new manifest, never edits one.

**Deployed done when:** `make integration` — one signed manifest round-trips against the real CMK, and a tampered body is rejected.

**Traps:** invariants 3–4 · [ADR-006](adr/ADR-006-approval-binds-to-digest.md) — the approval token will bind to exactly this digest · KMS `Sign` takes a digest, not the message, above 4 KB; get the message-type right or verification fails only in production.

## - [x] M4 · The remaining six participants, seeds, and generated ground truth

> **Complete 2026-07-26.** All eight handlers, the Meridian seed set, the measuring
> ground-truth generator (V8-12), conformance seeding/teardown for all eight (V8-13
> closed), and `erasure seed` / `erasure inspect`. `make seed` ran and was **validated
> against the services** (map == deployed discover == raw listings — the ground-truth
> consistency check in the deployed gate). `make conformance` run by the human:
> **56 passed / 8 skipped in 170.29s** — exactly the predicted SES-sandbox shape. The
> 8 skips are `notify-suppression`, whose seeding requires `PutSuppressedDestination`
> (sandbox-blocked); the suite refuses to grade what it couldn't seed rather than
> mocking it. They convert to passes (64 / 0) when production access lands — no code
> change, re-run only.

**Build:** `cognito_identity`, `profile_store`, `billing_ledger` (Aurora via RDS Data API), `vector_index` (S3 Vectors — [ADR-021](adr/ADR-021-s3-vectors-for-cost.md)), `analytics_lake`, `notify_suppression` · `seeds/` (the Meridian tenant and the seven subjects from the README table — Dmitri's litigation hold in `billing-ledger`, Yuki's injection payload in the `profile-store` bio, Nneka's `PARTIAL` from the SES suppression list) · `evals/fixtures/generator.py` **emitting the ground-truth placement map in the same pass it writes the data** ([ADR-020](adr/ADR-020-deployed-eval-gate.md)) · CLI `seed` and `inspect` become real.

**Hermetic done when:** `make check` green — all eight handlers unit-tested, registry complete.

**Deployed done when:** `make seed` then `make conformance` green 8/8 · a ground-truth consistency test proving the map matches what the services actually contain.

**Traps:** invariant 5 — seeded fake PII is treated as real everywhere; that discipline *is* the demo · eventual consistency is real now: the generator must wait on explicit consistency signals (GSI propagation, vector index visibility, Iceberg commit) rather than sleeping · `notify-suppression` must return `PARTIAL` for the retained suppression entry — which holds the **plaintext address**, not a hash (V8-1) — never `APPLIED` · `vector_index` has **no delete-by-query**: derive keys from `subjectRef`, batch at ≤500 per `PutVectors`/`DeleteVectors` call but **≤100 per `GetVectors`** (V8-2), and respect the per-index write ceiling when seeding the corpus · vector metadata is a PII surface and goes through the scrubber.

## - [x] M5 · The saga (LangGraph core — no model anywhere)

> **Complete 2026-07-27.** The full graph (11 nodes incl. `compensate`), reducers with
> concurrent-write tests, digest-bound KMS approval tokens, the hash-chained ledger,
> EventBridge one-shot scheduling with a stale-wake-filtering + deduplicating resume
> Lambda, and `infra/stacks/saga.py` (no `bedrock:*`, no VPC — synth-asserted). Phase 3
> runs ONE participant per superstep so each receipt checkpoints individually; a stuck
> participant is DLQ + a pause at the `stuck` gate (the diagram's "manual remediation"
> arc), never a rollback. Deployed gate run by the human: **6 passed in 140s** — all
> four scenarios plus M3's two, corroborated from outside the saga (tombstone row,
> verifying ledger chain, `UserNotFoundException`, zero profile items, re-request
> refused at intake).
>
> The gate earned its cost: three runs surfaced three defects `make check` could not
> reach — a Lambda asset that was not a function of its source (**V9-1**), a generator
> seam that only worked when entered through `run()` (**V9-2**), and a stray resume
> that could **wedge a live saga permanently** (**V9-3**). A fourth, residue from a
> fixture that failed during setup (**V9-4**), was found by inspecting the account
> afterwards. See [VALIDATION.md](VALIDATION.md).
>
> **Untested here, by disclosure:** in an SES-sandbox account the `RESIDUAL_BY_DESIGN`
> archetype is not exercised (no suppression entry to retain), and the suite warns
> rather than implying otherwise.

**Goal:** the StateGraph executes a **hand-written fixture manifest** end to end, in Lambda, checkpointed to DynamoDB. [ADR-001](adr/ADR-001-agent-proposes-saga-disposes.md) makes this possible: the saga replays manifests, so it is fully testable before discovery exists. Say that in the article.

**Build:** `saga/{state,graph,edges,checkpointer,handler}.py` + `nodes/` (intake, hold_check, plan, soft_delete, approval_gate with `interrupt()`, grace_window, **hold_recheck**, hard_delete, verify, sweep) · `compensate.py`, `ordering.py`, `tombstone.py` · `scheduler/{base,eventbridge,handler}.py` · `approval/{gate,tokens}.py` · `ledger/{chain,writer,verify}.py` · `infra/stacks/saga.py`.

**Hermetic done when:** reducer concurrency tests green · the no-model-client import test green · `cdk synth` asserts the `saga-executor` role has **no `bedrock:*`** (invariant 12).

**Deployed done when:** `make integration` — happy path with pause/approve/resume · **kill the executor mid-phase, re-invoke, resume from checkpoint with zero duplicate participant calls** · phase-2 failure → full compensation · phase-3 failure → no compensation, SQS DLQ, halt · post-approval manifest mutation → abort.

**Traps:** invariant 2 (no model client under `nodes/` — there's a test, and now an IAM denial) · invariant 6 (`restore` unreachable from phase 3) · invariant 10 (**every reducer gets a concurrent-write test** — a wrong reducer surfaces as a recall failure, not a crash) · invariant 11 (the resume handler is idempotent per `(thread_id, wake_reason)`; EventBridge Scheduler delivers at least once) · `thread_id` == `sagaId` · verify `interrupt()`/`Command(resume=…)` and `DynamoDBSaver`'s constructor against the installed pins before writing a line.

## - [x] M6 · Policy

> **Complete 2026-07-27.** The five-file Cedar set in `policies/cedar/` deploys as one
> `CfnPolicy` per statement behind a `CfnPolicyEngine`, each pinned to the one Gateway
> it governs, attached with `LOG_ONLY`/`ENFORCE` as a **CloudFormation parameter**
> (§9.4) and `validationMode=FAIL_ON_ANY_FINDINGS` so AWS refuses a policy that does
> not validate against the schema it generated. `make policy-test`: **42 passed**.
>
> **The research changed the design, and the record.** The generated schema exposes
> `context.input` — the tool's own arguments — and nothing else, and models each MCP
> tool as its own Cedar action. Six of §9.2's seven illustrative policies read facts
> that are not in the request and could never have fired. [ADR-024](adr/ADR-024-cedar-expresses-identity-and-shape.md)
> supersedes that policy set and names where each rule is actually enforced; §9.2 is
> kept, marked, as the record of what was intended.
>
> **The deploy taught four service-side rules the hermetic gate could not know**
> (VALIDATION V10-1..V10-4): Policy names take underscores; IAM rejects em dashes in
> descriptions; a tool-specific policy must pin its gateway ARN, which inverts the
> creation order to gateway → targets → policies; and attaching an engine makes the
> Gateway a caller needing `GetPolicyEngine` + `AuthorizeAction` +
> `PartiallyAuthorizeActions`. Each got a hermetic guard driven from the most
> authoritative artifact available, mutation-tested.
>
> **Deployed gate run 2026-07-27 in `ENFORCE`**: `make integration` green, and
> `scripts/verify_policy_gate.py` PASS on both probes — `tools/list` for an
> unpermitted identity returns an **empty surface**, and `hard_delete` with an empty
> approval token is **denied at the Gateway**, participant never invoked.


**Build:** `policy/{engine,schema,decisions}.py` — this line said `{engine,middleware,context,decisions,gateway}` and M6 was ticked without `middleware.py`, with no note saying so. [ADR-026](adr/ADR-026-no-middleware-seam.md) settles it: there is no middleware seam, because the model holds no tools. `context` and `gateway` folded into `schema.py` and `decisions.py`. · `policies/cedar/*.cedar` transcribed from ARCHITECTURE §9.2 · AgentCore Policy attachment in `infra/stacks/gateway.py` · deploy-time schema validation of the `.cedar` files against the Gateway's generated schema · `LOG_ONLY` vs `ENFORCING` as a **stack parameter**, not an env var.

**Hermetic done when:** `make policy-test` green — the Cedar files parse and evaluate, and the engine/Cedar divergence test passes.

**Deployed done when** (in `ENFORCE` — `POLICY_MODE=ENFORCE make deploy-dev`, riskless before M7 because nothing legitimate calls the Gateway yet): `make integration` still green · `scripts/verify_policy_gate.py` passes — a direct MCP `hard_delete` without a digest-bound token is **denied at the Gateway**, and `tools/list` for an unpermitted identity is **empty**.

> The gate as first written said "saga halts with no authz retry loop" and named the
> `asdp-discovery` tool surface. Both were drift, corrected here on the record: the saga
> never traverses the Gateway (it invokes participants directly — `saga/invoker.py`, M5),
> so a Gateway deny cannot halt it; and `asdp-discovery` is assumable only by
> `bedrock-agentcore.amazonaws.com`, so no caller can run `tools/list` *as* discovery
> until M7's Runtime exists. The discovery-surface claim is asserted hermetically today
> (`test_the_discovery_tool_surface_is_exactly_discover_and_verify`) and lands deployed
> as M7's `tool_surface_minimality` evaluator.

**Traps:** default-deny, forbid-wins · the engine and the Cedar files express identical rules against two backends — one divergence test between them · **entity and context names are validated against the generated schema, never assumed** ([ADR-018](adr/ADR-018-agentcore-policy.md)); a policy referencing a context key the Gateway does not inject is a policy that silently never fires · the decisions log feeds M7's adversarial eval.

## - [x] M7 · Discovery on AgentCore Runtime + the recall gate

> **Hermetic half landed 2026-07-27.** The discovery subgraph, the five agents, the
> Runtime HTTP contract, `infra/stacks/runtime.py`, Memory priors with the rejecting
> scrubber, `evals/run.py` + nine evaluators, and the adversarial corpus.
> `discovery/advisor.py` then wired the one model in the platform: its only channel is a
> scope-hint list, a hint can only *widen* a probe, and every failure path degrades to
> silence — so a model outage costs depth and never recall, and `make check` still needs
> no Bedrock. `make check`: **1330 passed**, mypy clean on 94 files — a count that now
> moves with the docs, since `test_doc_links.py` is parameterised per link (V10-9).
>
> **The research changed the artifact.** `CreateAgentRuntime` now accepts a
> `codeConfiguration` — an S3 zip — as well as a container image, and the container
> path would have put a Docker daemon inside `cdk synth`, which runs in `make check`.
> [ADR-025](adr/ADR-025-runtime-ships-a-code-zip.md) records that; the arm64 platform
> tag was proven with `pip download` first, because `numpy` publishes no
> `manylinux2014_aarch64` wheel and that would otherwise have been a fifth V10 finding.
>
> **Where recall comes from, stated plainly:** the prospector sweeps every registered
> participant on every run and the editor cannot subtract, so recall 1.0 is a property
> of the graph rather than of the model. That is also what makes the adversarial gate
> winnable without grading the model's disposition (§11.4).
>
> **Deployed gate run 2026-07-28.** `make eval` → **`✅ eval PASSED`**, which is printed
> only when every block survives: recall at `threshold=1.0` on **both** the cold and warm
> prior passes, plus precision, `hold_detection`, `manifest_completeness`,
> `no_premature_hard_delete`, `ordering_conformance`, `residual_honesty`, and the
> cross-cutting `tool_surface_minimality` and `no_pii_in_memory`. `make eval-adversarial`
> → **`✅ adversarial PASSED`**.
>
> **What that settles, and what it doesn't.** Recall 1.0 against eight real services is
> the gate ADR-008 exists for, and V5-1's open question — whether tool-selection accuracy
> degrades at eight participants behind one Gateway — now has evidence rather than a
> worry: it did not. What the run does *not* establish is behaviour at 80 participants,
> and the O(1) tool surface is the reason to expect it to hold, not a measurement of it.


**Build:** `discovery/subgraph.py` + `agents/` (cartographer, prospector, lineage, counsel, editor) · `runtime/entrypoint.py` (the AgentCore Runtime HTTP contract) + the arm64 code zip `make package` builds ([ADR-025](adr/ADR-025-runtime-ships-a-code-zip.md) — this line said "container image" until the research changed the artifact) · `discovery/advisor.py` (the one model, additive-only) · `infra/stacks/runtime.py` · AgentCore Memory priors with the pre-write scrubber ([ADR-019](adr/ADR-019-agentcore-memory-priors.md)) · `evals/run.py` and evaluators (recall **hard-fails below 1.0**, precision report-only, hold_detection, trajectory, residual_honesty, no_pii_in_memory, tool_surface_minimality) · the adversarial corpus end to end.

**Hermetic done when:** the read-only tool list is asserted at subgraph construction with a unit test behind it · `cdk synth` asserts the Runtime role has no participant IAM.

**Deployed done when:** `make eval` — recall 1.0, run both cold and warm on Memory priors · `make eval-adversarial` — pass criterion is *the tool was absent from the surface, or policy denied and logged*, never *the model resisted*.

**Traps:** invariant 1 enforced in three places now — subgraph construction, the Cedar permit, and Gateway tool-list filtering · invariant 8: a red gate means a better agent or a new fixture, never a lower threshold · invariant 13: a Memory write containing anything subject-shaped is **rejected**, not sanitised · priors are advisory — a prior may reorder discovery but may never cause a system to be skipped.

## - [x] M8 · The operator surface and the deployed walkthrough

> **Hermetic half landed 2026-07-28.** `approval/presenter.py` (ordering as the control),
> `approval/api.py` + `infra/stacks/api.py` (Cognito-authenticated, every route asserted),
> `cli/operations.py` + `cli/walkthrough.py`, and the six operator commands. `_UNBUILT` in
> `cli/main.py` is now **empty**, which the M0 docstring predicted would happen here.
>
> **Two read-only saga actions were needed and are the interesting part.** `describe` and
> `threads` let an operator see a paused saga *without* resuming it. LangGraph persists a
> `Command(resume=…)` against the pending interrupt before the node consumes it, so a
> "read" implemented as a no-op resume would wedge every saga an operator inspected — and
> would keep looking like a working read until the next legitimate approval (V9-3).
>
> **Approval has no bypass, deliberately.** `erasure approve` obtains a Cognito token and
> calls the authenticated route; missing operator credentials fail with the exact
> `admin-create-user` commands rather than falling back to invoking the Lambda. A fallback
> would have made every walkthrough green over a control nobody exercised.
>
> **Deployed gate run 2026-07-28/29.** Two clean arcs, corroborated in the tombstone
> registry rather than taken from the console output: `saga_9c21aa1fc511` (675s, 6
> systems) and `saga_13b51235e4ad` (627s, 4 systems). Different subjects by design — the
> second run picks a subject the first has not erased, because a deterministic choice
> would have aimed run 2 at run 1's tombstone (V11-7).
>
> **"Identically" means the shape, not the numbers.** Same eight steps, same gates, same
> ordering; the participant counts follow the subject and differ, which is the intended
> reading. A run whose *steps* varied would be describing a race.
>
> **Eight findings came out of this gate** (V11-1 … V11-8), and the deployed half found
> what the hermetic half could not: a saga behind a 30-second HTTP request, a scrubber
> that corrupted the digest invariant 3 binds to, a claims encoding assumed rather than
> read, a certificate assertion that could never pass. Three of them — V11-1, V11-3,
> V11-6 — were rules this repo had already written down in prose and never extended to
> the class they described.
>
> **The one deliberately-failed run was the most useful.** `saga_e18f65d4c942` halted at
> `BLOCKED_BY_HOLD` against the litigation fixture, which exercised the hold veto end to
> end and surfaced the seed-versus-`hold_check` disagreement about scoped holds now
> carried into M9.

**Build:** `approval/presenter.py` (anomaly-first: baseline diff and residual risk **first**, never a 400-row inventory) · `approval/api.py` and `infra/stacks/api.py` (Cognito-authenticated HTTP API for intake, approval, and operator reads) · CLI: `discover`, `walkthrough`, `threads`, `resume`, `approve`, `ledger`.

**Hermetic done when:** `make check` green with — the presenter puts **baseline diff and residual risk before any inventory row**, asserted by a test that fails on the reverse ordering (the trap below is that the presenter is a control, so "it renders" is not the property) · an approval request whose `manifest_digest` does not match the manifest is rejected **before a token is minted**, never after (invariant 3) · `cdk synth` asserts every route on the HTTP API carries the Cognito authorizer, since one unauthenticated approval route is the entire HITL control gone · each new CLI command exists and exits non-zero while unbuilt, per M0's convention.

> This gate was **missing** until M7 closed — M8 listed only a deployed gate, against the
> two-gate rule stated at the top of this file. Written now rather than at the first
> commit of M8, because a gate authored alongside the code it grades tends to grade what
> the code happens to do.

**Deployed done when:** `make walkthrough` runs the full arc against the dev stack — discover → soft delete → pause → *the executor Lambda returns and nothing is held* → `make threads` → `make approve` → grace → hard delete → certificate — cleanly, twice, identically.

**Traps:** rubber-stamping converts the HITL control into theatre; the presenter is a control, not a UI nicety · the walkthrough must show the pause as *absence of compute*, because that is the property [ADR-016](adr/ADR-016-serverless-durability.md) is built on · compress the grace window via a stack parameter, never by bypassing the scheduler.

## - [ ] M9 · Hardening: chaos + the upgrade canary

**Build:** the chaos suite from [PROJECT-STRUCTURE.md](PROJECT-STRUCTURE.md) (including Callum's resurrection at T+7 and a Scheduler double-fire → exactly one resume) · `tests/integration/test_upgrade_canary.py` implementing the `CANARY_STAGE=pause|resume` contract in `scripts/upgrade_canary.sh`'s header.

**Hermetic done when:** full `make check` green **with every gate now mandatory**.

**Deployed done when:** `make chaos` green · `bash scripts/upgrade_canary.sh` passes — pause a saga, bump the pinned `langgraph` **and `langgraph-checkpoint-aws`**, assert a clean resume from the same DynamoDB table.

> **Carried in from M8's deployed gate (V11-8).** `seeds/meridian.json` says the
> litigation hold is *"scoped to one table on purpose — the uploads and profile items are
> still erased"*, while `saga/nodes/hold_check.py` blocks the whole saga on any hold and
> says so deliberately (*"partial-scope erasure under a hold is a policy decision no
> default should make"*). Both are defensible; they cannot both be the design, and the
> fixture's `proves` field currently claims a property the platform demonstrates only
> half of. **Decide it here, with an ADR**, alongside the hold-during-grace-window case —
> that is the scenario where a scoped hold either does or does not stop phase 3 for the
> participants it never named.

**Traps:** the canary is [ADR-016](adr/ADR-016-serverless-durability.md)'s only control that actually catches a stranded saga — the script is the contract; the test implements it exactly · the canary must cover both pins, because serialization lives in the checkpoint package as much as in `langgraph` (VALIDATION baseline finding #3, in its new clothes).

## - [ ] M10 · Production posture

**Build:** `infra/stacks/observability.py` (alarms and dashboards for every metric in ARCHITECTURE §10.1) · AgentCore Evaluations wired against the dev stack as the drift monitor · cost controls (budget alarms; a synth-time check that no newly added service carries a provisioned floor — the rule [ADR-021](adr/ADR-021-s3-vectors-for-cost.md) established) · a documented teardown drill, including the Object Lock retention constraint · `.github/workflows/` ephemeral per-PR eval stack with an `always()` teardown step.

**Deployed done when:** the per-PR workflow creates, seeds, evaluates, and destroys a stack in one run, with no leaked resources · one documented production-shaped run with real Bedrock, its cost recorded · an idle dev stack left up for 24h costs cents, evidenced from Cost Explorer.

**Traps:** `make deploy` (prod) is human-only and denied in `.claude/settings.json` · teardown runs on `always()`, not on success — a failed run must not leak resources even though none of them now bill a floor · the break-glass merge path from [ADR-020](adr/ADR-020-deployed-eval-gate.md) is a logged exception, never a default.

---

## After the build: article assets

Capture while everything is fresh: the `make walkthrough` transcript including the moment the Lambda returns mid-saga and nothing is running · `make diagrams` SVGs (lead with 04-recovery-semantics) · the policy-deny log line from Yuki's injection **and** the `tools/list` response proving the tool was never offered · the KMS 7-day-window finding that moved the shred down a layer · the VALIDATION.md findings table. The build history itself — 009 → 011 → 013 on framework, 003 → 014 → 016 on durability, 012 → 017 on participants, defects found and fixed on the record — is article material, not laundry to hide.
