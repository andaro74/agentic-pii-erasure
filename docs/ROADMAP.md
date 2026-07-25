# Build roadmap

The repository is **docs-first by design**: `docs/` describes the finished system; `src/` starts near-empty. This file defines the order in which the system gets built. Each milestone is sized for one to a few Claude Code sessions and has an **executable "done when"** — a command, not an opinion.

## Rules of the build

1. **Work the first unchecked milestone** unless the human names another. Tick the box only after "done when" passes.
2. **Never build ahead.** The docs describe the target; building target features early creates untested claims — the exact defect class the validation pass caught four times ([VALIDATION.md](VALIDATION.md)).
3. **Verify APIs against the installed pinned versions** before writing framework code. The pins are exact for a reason (ADR-014); remembered signatures are not evidence.
4. **`make check` stays green at every commit.** Unbuilt stages print "⏳ lands at Mx"; when a milestone lands, its gate becomes mandatory automatically. Never re-add a guard to silence a failing gate.
5. **Docs move with code.** Drift found or created gets fixed in the same commit, or a superseding ADR is written.

## When each `make` target lights up

| Target | Milestone | | Target | Milestone |
|---|---|---|---|---|
| `install` `lint` `fmt` `test` | M0 | | `policy-test` | M6 |
| `conformance` | M2 | | `eval` `eval-adversarial` | M7 |
| `seed` `inspect` | M4 | | `demo-offline` `threads` `resume` | M8 |
| `integration` | M5 | | `upgrade-canary` | M9 |
| | | | `demo` (real Bedrock) `synth` | M10 |

---

## - [ ] M0 · Walking skeleton

**Goal:** the package installs, imports, and has a CLI. Everything after this is adding organs to a living body.

**Build:** `cli/main.py` (typer app: `--help`, `version`; `seed`/`demo` print "⏳ lands at M4/M8" and exit non-zero — a stub that pretends success violates the repo's ethos) · `observability/logging.py` + `redact.py` skeleton (structlog config, scrubber with tests) · package `__init__.py` files.

**Done when:** `make install && make check` green · `.venv/bin/erasure --help` shows the app.

**Traps:** none structural — but redact.py is invariant 5's mechanism, so even the skeleton gets a test proving an email never survives the scrubber.

## - [ ] M1 · The contract

**Goal:** `contract/` — the package everything depends on and the highest-risk file in the repo.

**Build:** `verbs.py`, `archetypes.py`, `outcomes.py`, `registry.py`, `idempotency.py`, and `canonical.py` with a serious test file: shuffled key order → identical bytes; semantically-unordered arrays sorted by defined key; numeric form normalisation; **no timestamps or run IDs in the digested body**.

**Done when:** `make test` green with the canonicalisation stability suite · `mypy --strict` clean.

**Traps:** invariant 4. Any later change to canonicalisation is a breaking change requiring a `schemaVersion` bump and a fixture. Get the property-style tests in *now* — ADR-006's digest binding is only as strong as this file.

## - [ ] M2 · Base harness + first two participants + conformance suite

**Goal:** prove the five-verb contract with one easy participant and one that can't lie.

**Build:** `participants/_base/{server,store,holds}.py` (MCP harness, JSON store with an applied-idempotency-key log) · `vault_files` (blob + versioning: delete marker ≠ deletion) · `aegis_archive` (WORM: `hard_delete` = destroy the per-subject DEK; after shred, decryption failure is distinguishable from not-found) · `tests/conformance/` **parameterised over the registry** — never bespoke per participant.

**Done when:** `make conformance` green for both.

**Traps:** invariant 7 (`PARTIAL` + `residual`, never a hopeful `APPLIED`) · conformance asserts `discover` is side-effect-free via state snapshot diff · replayed idempotency key → `ALREADY_APPLIED`, not double-apply · ADR-007's trap: the DEK registry is excluded from any backup/copy path, asserted by test.

## - [ ] M3 · Manifest + signing

**Build:** `manifest/{models,digest,signing,validate}.py` — Pydantic v2 models, digest over `canonical()` of the body (provenance excluded), Ed25519 locally, immutability after signature.

**Done when:** unit tests: mutate any field → digest changes; provenance changes → digest identical; sign/verify round-trip; re-plan produces a new manifest, never edits one.

**Traps:** invariants 3–4 · ADR-006 — the approval token will bind to exactly this digest.

## - [ ] M4 · Seeds, ground truth, remaining six participants

**Build:** `seeds/` (Meridian tenant + the seven subjects from the README table — Dmitri's litigation hold in `ledger_billing`, Yuki's injection payload in the CRM bio, Nneka's `PARTIAL` in `pigeon_comms`) · `evals/fixtures/generator.py` **emitting the ground-truth placement map in the same pass it writes the data** · `helios_crm`, `ledger_billing`, `atlas_identity`, `beacon_search`, `quarry_lake`, `pigeon_comms` · CLI `seed` and `inspect` become real.

**Done when:** `make seed` then `make conformance` green 8/8 · ground-truth consistency test (map matches what participants actually contain).

**Traps:** invariant 5 — seeded fake PII is treated as real everywhere; that discipline *is* the demo · ADR-012: generated-not-labelled ground truth is what makes M7's recall gate trustworthy.

## - [ ] M5 · The saga (LangGraph core — no model anywhere)

**Goal:** the StateGraph executes a **hand-written fixture manifest** end to end. ADR-001 makes this possible: the saga replays manifests, so it is fully testable before discovery exists. Say that in the article.

**Build:** `saga/{state,graph,edges,checkpointer}.py` + `nodes/` (intake, hold_check, plan, soft_delete, approval_gate with `interrupt()`, grace_window, **hold_recheck**, hard_delete, verify, sweep) · `compensate.py`, `ordering.py`, `tombstone.py` · `approval/{gate,tokens}.py` · `ledger/{chain,writer,verify}.py`.

**Done when:** `make integration` — happy path with pause/approve/resume · **kill the process mid-phase, restart, resume from checkpoint with zero duplicate participant calls** · phase-2 failure → full compensation · phase-3 failure → no compensation, halt · post-approval manifest mutation → abort.

**Traps:** invariant 2 (no model client under `nodes/` — there's a test) · invariant 6 (`restore` unreachable from phase 3) · invariant 10 (**every reducer gets a concurrent-write test** — a wrong reducer surfaces as a recall failure, not a crash) · `thread_id` == `sagaId` · verify `interrupt()`/`Command(resume=…)` signatures against the installed `langgraph==` pin before writing a line.

## - [ ] M6 · Policy

**Build:** `policy/{engine,middleware,context,decisions}.py` · `policies/cedar/*.cedar` transcribed from ARCHITECTURE §9.2 · `tests/unit/test_policies.py` · `LOG_ONLY` vs `ENFORCING` via env.

**Done when:** `make policy-test` green · integration: `hard_delete` without a digest-bound token → **denied and logged**, saga halts with no authz retry loop.

**Traps:** default-deny, forbid-wins · the engine and the Cedar files express identical rules against two backends — one divergence test between them · decisions log feeds M7's adversarial eval.

## - [ ] M7 · Discovery + the recall gate

**Build:** `discovery/{subgraph,stub_model}.py` + `agents/` (cartographer, prospector, lineage, counsel, editor) · `evals/run.py`, evaluators (recall **hard-fails below 1.0**, precision report-only, hold_detection, trajectory, residual_honesty, no_pii_in_memory) · adversarial corpus end-to-end.

**Done when:** `make eval` — recall 1.0 offline · `make eval-adversarial` — pass criterion is *policy denied and logged*, never *the model resisted*.

**Traps:** invariant 1 enforced in code: read-only tool list asserted at construction, unit test behind it · invariant 8: a red gate means a better agent or a new fixture, never a lower threshold · stub model must be deterministic — CI depends on it.

## - [ ] M8 · The demo and the operator surface

**Build:** `approval/presenter.py` (anomaly-first: baseline diff and residual risk **first**, never a 400-row inventory) · CLI: `discover`, `demo`, `threads`, `resume`, `approve`, `ledger`.

**Done when:** `make demo-offline` runs the full arc — discover → soft delete → pause → *kill the process* → `make threads` → `make resume` → grace → hard delete → certificate — cleanly, twice, identically.

**Traps:** rubber-stamping converts the HITL control into theatre; the presenter is a control, not a UI nicety · demo touches no AWS, ever.

## - [ ] M9 · Hardening: chaos + the upgrade canary

**Build:** the chaos suite from PROJECT-STRUCTURE (including Callum's resurrection at T+7 and timer double-fire → exactly one resume) · `scheduler/{base,local}.py` · `tests/integration/test_upgrade_canary.py` implementing the `CANARY_STAGE=pause|resume` contract in `scripts/upgrade_canary.sh`'s header.

**Done when:** full CI green **with every guard now mandatory** · `bash scripts/upgrade_canary.sh` passes locally.

**Traps:** the canary is ADR-014's only control that actually catches a stranded saga — the script is the contract; the test implements it exactly.

## - [ ] M10 · (Optional) The AWS path

**Build:** `scheduler/{eventbridge,handler}.py` (idempotent per `(thread_id, wake_reason)` — invariant 11) · Postgres checkpointer wiring · real-Bedrock `make demo` · `infra/` CDK stacks per `infra/README.md`.

**Done when:** `make synth` clean · one documented real-model run.

**Traps:** `make deploy` is human-only (denied in `.claude/settings.json`) · Cedar entity names validated against the Gateway's generated schema, not assumed.

---

## After the build: article assets

Capture while everything is fresh: the `demo-offline` transcript including the kill/resume moment · `make diagrams` SVGs (lead with 04-recovery-semantics) · the policy-deny log line from Yuki's injection · the VALIDATION.md findings table. The build history itself — 009 → 011 → 013, defects found and fixed on the record — is article material, not laundry to hide.
