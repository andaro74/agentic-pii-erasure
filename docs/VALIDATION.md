# Validation log

This repo is **docs-first**: `docs/` describes the finished system; `src/` is built
up milestone by milestone. That gap is a hazard. The characteristic defect is not a
crash — it is an **untested claim**: an assertion, gate, pin, or fixture that *looks*
like it enforces something but cannot actually catch the failure it names. Building
ahead of the current milestone manufactures these, because the claim ships before the
thing that would make it true.

This file is the running record of validation passes that hunt that defect class. It
is a living log, not a one-time audit.

> **Provenance.** The "Baseline pass" findings below are reconstructed to match the
> record referenced across [CLAUDE.md](../CLAUDE.md) and [ROADMAP.md](ROADMAP.md),
> which cite this log as having caught the same defect class four times. Later passes
> are recorded as they happen, dated.

## The defect class, stated once

> A control that cannot fail proves nothing. A test that can't go red, a gate that
> can't block, a pin that doesn't cover the layer that actually breaks, a fixture
> whose ground truth is copied from the output it grades — each *reads* as a
> safeguard and is in fact decoration.

The fix is never to relax the control. It is to make the control able to fail, then
make the system pass it for real. ROADMAP rule 2 and CLAUDE.md's invariants exist
because of what this log found.

## Baseline pass — four findings, one defect class

| # | Finding | Why it was decoration | Fix | Now guarded by |
|---|---|---|---|---|
| 1 | **A test that couldn't pass** — a unit test asserting behaviour for a milestone whose implementation did not exist yet (assertion built ahead of the code). | It failed or was skipped; either way it backed a claim nothing implemented. | Only write a milestone's tests with its code; "done" means the test is green on real output. | ROADMAP rule 1–2; `make check` at every commit |
| 2 | **A gate that couldn't gate** — a `make` target guarded on a file that didn't exist yet, so it exited 0 and silently gated nothing. | A green gate that never evaluated anything reads as "covered" when it covered nothing. | Milestone gates print `⏳ lands at Mx` until the stage's entry file exists, then become mandatory **automatically**; never re-add a guard to silence a real failure. | Makefile milestone-gate pattern; ROADMAP rule 4 |
| 3 | **A pin protecting the wrong layer** — `langgraph` pinned exactly, but the checkpoint/serialization packages left ranged. | Serialization lives in the checkpoint packages as much as in `langgraph`; a ranged bump there could still strand a paused saga past a deadline — the exact thing the pin was meant to prevent. | Pin `langgraph` **and** `langgraph-checkpoint-*` in lockstep; gate every bump with `make upgrade-canary`. | Invariant #9; [ADR-014](adr/ADR-014-langgraph-owns-durability.md); `pyproject.toml` pin comment |
| 4 | **A fixture that couldn't fail** — recall ground truth hand-aligned to the discovery agent's output, making `make eval` a tautology (recall trivially 1.0). | A gate graded against a copy of its own answer key can never go red, so a real recall regression would sail through. | Generate ground truth in the **same pass** that writes the seed data; discovery runs blind against it. | Invariant #8, #10; [ADR-008](adr/ADR-008-recall-1.0-hard-gate.md), [ADR-012](adr/ADR-012-simulated-participants.md) |

All four are the same bug wearing different clothes: a safeguard that cannot exercise
the failure it advertises.

## Pass log

### 2026-07-24 · Doc-completeness + build-tooling pass (pre-M0)

Adversarial read of the docs against the actual tree while standing up the build
environment. Findings:

- **Supporting-doc layer referenced as existing but absent.** `docs/adr/` (12 ADRs
  cited, 7 by direct link), `docs/VALIDATION.md` (this file), and `docs/diagrams/`
  (`04-recovery-semantics.mermaid` linked from the README) were all treated as
  authoritative throughout the docs but were never committed. The ADR gap is the
  most consequential — CLAUDE.md's "read the ADR before contradicting it / write a
  superseding ADR" workflow was inoperable. **Fixed this pass:** ADR set, this log,
  and the diagram series created from the decisions already tabulated in
  ARCHITECTURE.md §15 and §6.
- **A gate that couldn't gate (instance of baseline #2), on Windows.** `make lint`
  ran `ruff check src tests evals seeds`; `evals/` and `seeds/` don't exist until
  M4/M7, so ruff errored `E902` and `make check` could not go green at commit zero —
  contradicting the ROADMAP claim. **Fixed:** `LINT_DIRS := src tests $(wildcard
  evals seeds)` lints only existing dirs and picks up the rest automatically when
  they land — the same "mandatory when it lands" rule as the milestone gates, not a
  silencing guard.
- **`.venv/bin` hardcoded in the Makefile.** Broke every tool target on native
  Windows (venv is `.venv/Scripts`). **Fixed:** `VENV_BIN` auto-detect, portable
  across Windows/WSL/Linux/CI.

Net: `make check` green at commit zero, and the doc set the invariants depend on now
exists.

### 2026-07-25 · AWS-native serverless rewrite (architecture v0.1 → v0.2)

The architecture moved to AWS-only, serverless, on Amazon Bedrock AgentCore. Six superseding
or refining ADRs (015–020) and a full pass over the doc set. Findings, in the sense this log
cares about — controls that could not fail, and claims nothing backed:

- **A claim the docs made in two directions at once.** ARCHITECTURE §4.2 already listed seven
  *real AWS services* as the participant archetypes and §11.2 opened "Because all participants
  are real AWS services…", while [ADR-012](adr/ADR-012-simulated-participants.md), CLAUDE.md
  and the README described eight *fictional* subsystems over local JSON. Both could not be
  true. The rewrite resolves it in favour of real services
  ([ADR-017](adr/ADR-017-real-aws-participants.md)) rather than papering over the older text.
  Pre-existing drift, found by reading the doc set as one document instead of eight.
- **Two invariants that were conventions wearing a test.** Invariant 2 ("the saga never
  re-enters the model") was an import test, and the DEK registry's backup exclusion was prose
  plus a promised test. Both are now *also* infrastructure assertions that run in `make check`:
  the `saga-executor` role has no `bedrock:*` (invariant 12), and the DEK table has PITR
  disabled with no AWS Backup selection (invariant 14). An import test cannot catch a widened
  IAM policy; a `cdk synth` assertion can.
- **A real service constraint that changed the design, which no simulation would have
  surfaced.** `kms:ScheduleKeyDeletion` enforces a minimum 7-day pending window. Crypto-shred
  implemented as "destroy the KMS key" could therefore never return `APPLIED` inside a
  one-month statutory deadline — it would return `PARTIAL` with a multi-week residual and the
  Certificate of Erasure would be unissuable. The shred moved down a layer, to deleting the
  wrapped per-subject DEK. This is the clearest single argument for ADR-017 and it is recorded
  in [ADR-007](adr/ADR-007-crypto-shredding-for-worm.md).
- **The gate we gave up, stated as a loss rather than a footnote.** ADR-012's strongest
  argument — *a merge gate must not depend on a cloud service being reachable* — is now
  violated for conformance, integration, and the recall gate.
  [ADR-020](adr/ADR-020-deployed-eval-gate.md) records it as a cost, keeps the property that
  actually made the gate trustworthy (ground truth generated in the same pass that writes the
  data, never labelled), and documents break-glass as a *logged exception, not a default*.
  The tempting wrong answer — mock the participants so the eval stays hermetic — is baseline
  finding #4 in new clothes, and is rejected explicitly.
- **A pin that had drifted from the thing it protects.** Baseline finding #3 fixed
  `langgraph-checkpoint-sqlite` being ranged. The move to a DynamoDB checkpointer would have
  reintroduced the identical defect if only `langgraph` stayed pinned, so invariant 9 now names
  `langgraph-checkpoint-aws` explicitly and the upgrade canary covers both.

Open, and deliberately not resolved this pass: the 15-minute Lambda ceiling on a saga phase
(§16 Q5), whether the upgrade canary is sufficient for a younger checkpoint package (§16 Q6),
and whether the OpenSearch Serverless OCU floor is worth the derived-index archetype (§16 Q7).
Each is a real trade with no obviously right answer; marking them open is the point.

Net: `make check` is still hermetic and green at commit zero with no AWS account, and the
controls that used to depend on reviewer discipline now fail in CI.

### 2026-07-25 · Cost pass — the derived-index participant (§16 Q7 closed)

A targeted pass on one question the previous entry left open, driven by the observation that
the cheapest way to use a repo with no local mode had become *not deploying it*.

- **Q7 is answered, and the answer is recorded rather than the question deleted.** Participant
  #6 moved from OpenSearch Serverless to **S3 Vectors** purely on cost
  ([ADR-021](adr/ADR-021-s3-vectors-for-cost.md)). OpenSearch Serverless bills a continuous OCU
  floor *for existing*, not for working, and in a stack where everything else scales to zero it
  dominated the bill by an order of magnitude — Bedrock included. §16 Q7 is struck through and
  marked resolved in place; ADR-017's "Cost 2" bullet is likewise amended rather than rewritten.
- **A constraint became a rule.** "No component may bill continuously for existing rather than
  for working" is now stated in ARCHITECTURE §1.2, CLAUDE.md, and `infra/README.md`, with the
  follow-through that any new AWS service carrying a provisioned floor needs an ADR arguing for
  it. A cost property nobody wrote down is a cost property that drifts back.
- **A second real-service constraint surfaced, of the same class as the KMS 7-day window.**
  S3 Vectors has **no delete-by-query** — `DeleteVectors` takes keys, ≤500 per call. So
  `vector-index` derives keys deterministically from `subjectRef` rather than depending on a
  side mapping table, and §5.2's "keep the identifier alive until last" stops being advice: lose
  the join key and the embeddings are fully present and permanently unaddressable. A simulated
  participant would have offered a convenient `delete_where()` and taught nothing. Added to the
  §12 failure matrix.
- **The archetype's lesson changed, and the docs say so instead of claiming a pure win.** The
  soft delete has no alias to hide behind (it is a metadata flag every reader must filter on),
  and the derived artifact is an embedding — which is itself personal data, recoverable in part
  from the vector. Some loss (full-text semantics), some gain (RAG-shaped, closer to what
  readers are building). ADR-021 states both directions and records that the decision was made
  on cost regardless.
- **Latency is a stated limit, not a silence.** S3 Vectors is not a low-latency serving store;
  the AWS-documented tiering pattern behind OpenSearch would reintroduce the floor for a hot
  tier. Recorded in ADR-021's alternatives and the README's known limits so nobody reads this as
  "S3 Vectors replaces OpenSearch generally."

No finding of the defect class this log exists for — no control was weakened, and the cost
change did not touch a gate. `make check` remains hermetic and green.

### 2026-07-25 · Validation pass over ADRs 015–021 and the v0.2 rewrite

Adversarial read of everything changed since the two entries above, per the discipline below.
Governing question applied to every claim: *would this actually execute?*

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V3-1 | **High** | **A gate that couldn't gate (baseline #2, again):** `.github/workflows/ci.yml` claimed an ephemeral per-PR eval stack (`PII_ERASURE_STAGE: pr-<run_id>`), but `make deploy-dev`/`destroy-dev` hardcoded `--context stage=dev`. Every "ephemeral" run would have deployed into — and then **destroyed** — one shared stack; two concurrent PRs would clobber each other, and the isolation ADR-020 depends on did not exist. | **Fixed.** Makefile `STAGE ?= $(or $(PII_ERASURE_STAGE),dev)` now flows the CI env var into the CDK context; deploy-dev/destroy-dev target `stage=$(STAGE)`. Humans still get `dev` by default. |
| V3-2 | Medium | **A claim without its mechanism:** `pyproject.toml` and ADR-016 control #1 both asserted "a committed lockfile covers the transitive layer." No lockfile exists in the repo. Same family as baseline #3 — invariant 9's pin protects two direct dependencies while the text claimed the whole tree was covered. | **Fixed** by making the claim honest rather than quietly satisfying it with a stale freeze: both texts now state the transitive layer is *not yet* covered, and the lockfile is a named ROADMAP M0 deliverable with its own done-when clause ("CI installs from it"). |
| V3-3 | Medium | **Stale vocabulary in an Accepted ADR:** ADR-008 still said the recall gate is "hermetic in CI" and costs "a deterministic stub model" — both removed by ADR-017/020 — and its status line lacked the "refined by 020" marker the ADR index already claimed for it. An Accepted ADR contradicting two newer Accepted ADRs is exactly the silent divergence the ADR discipline forbids. | **Fixed.** Status line carries the refinement; the hermetic/stub-model passages now record what changed and where the gate runs, with the original cost preserved as history. |
| V3-4 | Low | **A trigger that could miss its event:** the upgrade-canary CI job detected pin bumps with `git diff HEAD~1`, which only inspects the last commit — a bump buried in a multi-commit push to main would skip the canary, the one control that catches a stranded saga. | **Fixed.** Event-aware diff range: PRs diff their merge-base with the target branch; pushes diff `github.event.before..HEAD`; `HEAD~1` remains only as the first-push fallback. |

Checked and clean (no finding): all relative markdown links resolve (0 broken); the three
Mermaid blocks embedded in ARCHITECTURE.md are body-identical to their `docs/diagrams/`
sources; code-fence counts balance in every touched doc; every invariant number cited in
ARCHITECTURE/ROADMAP/the ADRs maps to the CLAUDE.md invariant it names; the framework-import
allowlist is verbatim-identical between CLAUDE.md and PROJECT-STRUCTURE.md; every `make`
target referenced in docs exists in the Makefile; ADR-021's S3 Vectors facts (GA Dec 2025,
500-vector call limit, no delete-by-query, metadata ceilings) were verified against current
AWS documentation rather than recalled; `.claude/settings.json` parses and its deny list
covers every money-spending target the docs call human-only. `make check` green throughout.

V3-1 is the pass's teaching example: it was introduced *in this same session* that wrote
"a gate that couldn't gate" into the log twice. The defect class does not care who wrote
the control or how recently the author re-read the definition.

1. Read a doc claim as an adversary: *what would make this false, and could the
   named control detect it?*
2. If the control can't go red, that's a finding — record it here with the fix and
   the guard that now backs it.
3. Never close a finding by weakening the control. Make it able to fail, then pass it.
