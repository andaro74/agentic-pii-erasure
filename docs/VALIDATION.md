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

### 2026-07-26 · First-run pass — walking `infra/README.md` as a new user (post-M0)

Not a document review. The human ran the documented path on a clean machine and hit a wall
the docs did not mention, which is a category of defect no amount of re-reading finds: the
docs were internally consistent and still could not be followed.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V4-1 | **High** | **A setting with no mechanism:** `.env.example` documented `AWS_REGION` under a heading claiming the file "configures the CLI and the deploy", and nothing read it. `make` does not source `.env`, and the stacks are environment-agnostic by design, so the CDK CLI resolved the region from **ambient credentials**. A user setting `eu-west-1` in `.env` with `us-east-1` in their profile deploys to `us-east-1` — a regional stack in a region they did not choose, where AgentCore or their Bedrock model may not exist, surfacing as a discovery failure at M7 rather than as a deploy error. The docs' own region prerequisites were unenforceable. | **Fixed.** `bootstrap`/`deploy-dev`/`deploy`/`destroy-dev` source `.env` explicitly and **fail loudly** on an unset region rather than falling back. An exported shell variable wins over the file, so CI keeps supplying `AWS_REGION` and a per-run `PII_ERASURE_STAGE` — sourcing blindly would have resurrected V3-1, because `make install` writes a `.env` from the example. `make synth` deliberately does not load it: it is part of the hermetic gate and must stay credential-free. Backed by `tests/unit/test_makefile_env.py`, which asserts the wiring on every AWS-touching target and *executes* the fragment to prove the precedence. |
| V4-2 | Medium | **A prerequisite documented nowhere:** `cdk bootstrap` appeared in no `.md` in the repo and had no `make` target. The documented first run — prerequisites → `make deploy-dev` — cannot work on a fresh account. | **Fixed.** `make bootstrap` (human-only, denied in `.claude/settings.json`) resolves the account from STS and the region from `.env`; `infra/README.md` gains a numbered first-run table naming, for each step, *the symptom you get by skipping it*, because none of them fail where the mistake was made. |
| V4-3 | Low | **An inconsistency that only bites off the happy path:** `deploy-dev`/`deploy`/`destroy-dev` relied on `cdk.json`'s `app: python app.py`, so from a shell without the venv activated they fail with `ModuleNotFoundError: aws_cdk` — the exact footgun `make synth` already avoided by passing the venv interpreter explicitly. | **Fixed.** All CDK targets now pass `--app '$(CDK_APP)'`, and the pinned CLI version lives in one `$(CDK)` variable instead of five copies. |

**The fix's own first attempt was the same defect class again.** Shell precedence was first
implemented as `eval "$(export -p)"` — snapshot the environment, restore it after sourcing.
It reads as more general and it is broken on Windows, where environment variables named
`ProgramFiles(x86)` are not valid shell identifiers and the `eval` fails, silently leaving
`.env` in charge. The behavioural test caught it on the machine it was written on, before
it reached a deploy. Precedence is now applied to the two variables by name.

Both guards were **mutation-tested**: with `LOAD_ENV` replaced by a naive `. ./.env`, the two
precedence cases go red. A guard nobody has watched fail is a guard nobody has tested.

### 2026-07-26 · Follow-up to V4-1 — the rest of the settings with no mechanism

V4-1 fixed one inert variable. Asking "how many others are there" was a question nobody
had asked, and the answer was six — which makes the finding a *class*, not an incident.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V4-4 | Medium | **A setting that can never be wired, and five that are not wired yet, with nothing distinguishing them.** `PII_ERASURE_STACK_PREFIX` was read by nothing and never will be: `asdp-` is a literal in `infra/app.py`, the ADRs, `infra/README.md` and the synth assertions. It reads like isolation and provides none — two deployments in one account under different prefixes would collide, because the second `make deploy-dev` updates the first one's stack. That is V3-1's failure shape wearing different clothes; `PII_ERASURE_STAGE` is the mechanism that actually separates them. Separately, `PII_ERASURE_MODEL_ID`, `PII_ERASURE_POLICY_MODE`, `PII_ERASURE_TENANT`, the two timer variables and `OTEL_SERVICE_NAME` are all legitimately unconsumed — their milestones are unbuilt — but nothing in the file said so, so a reader could not tell "not built yet" from "you typed it wrong". Worst of them: `Makefile`'s `seed` target hardcoded `--tenant meridian` three lines from a `PII_ERASURE_TENANT` nothing read. | **Fixed.** The prefix is deleted; every not-yet-consumed variable carries a `⏳ lands at Mx` marker naming its milestone; `seed` reads `$PII_ERASURE_TENANT`. Backed by `tests/unit/test_env_example.py`, which parses `.env.example` and fails unless each variable is *either* referenced under `src/`, `infra/`, the `Makefile` or the workflow, *or* carries a marker — so the class cannot recur silently. Mutation-tested: re-adding the prefix and inventing a knob produce three failures. |

The generalisation worth keeping: **V4-1 was found by walking the documented path, V4-4 by
asking what else shares its shape.** The first needs a human on a clean machine; the second
is a fifteen-line parser. Both are cheaper than the deploy that goes to the wrong region.

### 2026-07-26 · M2 — building the Gateway contradicted a documented claim

Not found by reading. Found by reading the AgentCore developer guide closely enough to
write the target configuration, which is a different activity from re-reading our own
docs and reaches a different class of defect.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V5-1 | **High** | **An architectural claim the chosen mechanism cannot deliver.** ARCHITECTURE §4 stated that the agent "never learns that there are eight backends" and that the tool surface stays **O(1) in participant count** — and justified it as protecting tool-selection accuracy, which protects recall, which is invariant 8. AgentCore Gateway publishes every tool as `${target_name}___${tool_name}`. With one target per participant (which §4 also mandates, two sentences earlier), eight participants publish **forty** tools whose names enumerate the backends. The two sentences could not both be true, and the false one was load-bearing for a claim about the metric that must not move. | **Corrected on the record, not deleted.** §4 now states what the Gateway actually provides — one endpoint, one protocol, one authorization point, and per-identity tool filtering that keeps a discovery identity to `O(2N)` rather than `O(5N)` — and states plainly that O(1) is not among them. The alternative that would restore it (a single routing target) is described together with its cost: one Lambda holding the union of every participant's permissions, against §9.3's one-role-per-participant separation. Deferred to M7's eval, where tool-selection degradation would actually show up, rather than pre-empted. |

**Why this one is worth the space.** The pattern is the same as V4-1's — a claim whose
mechanism was never checked — but the checking looked different. V4-1 needed a human to
walk the documented path; V5-1 needed someone to read the *service's* documentation
rather than ours. ROADMAP rule 3 ("verify the API shape against current documentation,
not memory") was written to prevent writing wrong code. It also catches wrong docs.

Also fixed while building, each caught by a tool rather than by review: `cdk synth`'s
CloudFormation validator rejected an em dash in an IAM role description (the field is
restricted to printable Latin-1); a `Code.from_asset` path relative to the working
directory resolved differently under `make synth` (which runs in `infra/`) and under
pytest (repo root), which fails as "cannot find asset" in whichever you did not try
first; and `moto` surfaced a corrupted `cffi` wheel in the venv rather than a bug.

### 2026-07-26 · Pre-flight verification of M5's instructions (requested before starting)

The human asked for M5's roadmap entry to be verified before work began. Every claim was
checked against the thing that could falsify it, per ROADMAP rule 3:

- **Framework APIs, against the installed pins:** `StateGraph`/`START`/`END` at
  `langgraph.graph`, `interrupt`/`Command` at `langgraph.types` (langgraph 1.2.9), and
  `DynamoDBSaver(table_name, …, ttl_seconds, s3_offload_config)` in
  langgraph-checkpoint-aws 1.2.0 — matching the checkpoint table M0 synthesised.
- **File and node lists, across four documents:** ROADMAP M5's build list is
  file-for-file identical with PROJECT-STRUCTURE's `saga/`, `scheduler/`, `approval/`
  and `ledger/` trees, and its ten node names match ARCHITECTURE §5's state diagram,
  including `hold_recheck` and `sweep`.
- **Invariant citations:** 2, 6, 10, 11, 12 all exist in CLAUDE.md under those numbers
  and say what M5 cites them for. `thread_id == sagaId` is consistent everywhere.
- **The apparent contradiction that is not one:** PROJECT-STRUCTURE says `plan.py`
  invokes the Runtime; the Runtime lands at M7. M5's own goal resolves it — the saga
  executes a *hand-written fixture manifest* (ADR-001), which is what makes the saga
  testable before discovery exists.
- **moto's KMS**, since M3's hermetic gate depends on it: `ECC_NIST_P256` sign/verify
  with `MessageType="DIGEST"` round-trips and rejects a tampered digest.

**No findings.** One structural note, acted on rather than logged as a defect: M5
imports `manifest/`, which is M3's deliverable (`contract ← manifest ← saga`), so
"start M5" was executed as M3 first. M4 is deferred behind M5 by explicit human
instruction — legitimate under ROADMAP rule 1, and workable because M5's fixture
manifest needs only the two participants M2 built.

> **Retraction, same day.** The human clarified that "start M5" was a typo for "start
> M3" — which is what had been built anyway, since M3 was M5's prerequisite. The
> deferral of M4 is therefore void and the book order resumes: M4 is next. Kept on the
> record rather than edited away, because a log that rewrites its reasoning is not a log.

### 2026-07-26 · V6-1 — an entire package silently excluded from the repository

Caught by CI, not by anything local, and it arrived wearing a disguise.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V6-1 | **High** | **`.gitignore` excluded `src/pii_erasure/manifest/` — M3's entire deliverable.** The stock Python template's `MANIFEST` pattern (meant for setuptools' generated file at the repo root) is unanchored, so on a case-insensitive filesystem git matched it against the *directory* `manifest/`. Five modules and the golden fixture directory `tests/fixtures/manifest/` were excluded from the commit. `git add -A` said nothing; `git status` was clean; `make check` passed locally because the files exist on disk. A clone would not have contained the manifest layer at all. | **Fixed.** The pattern is now `/MANIFEST` — anchored to the root and to a file, which is what setuptools actually generates. The package and the fixture are committed. Backed by `tests/unit/test_nothing_source_is_ignored.py`, which fails if any authored file under `src/`, `tests/`, `infra/stacks/`, `evals/`, `seeds/` or `policies/` is gitignored, and separately if any `__init__.py` on disk is untracked. |

**The disguise is the lesson.** `make check` runs lint before tests, so the missing
package surfaced in CI as **`I001 Import block is un-sorted`** — because ruff resolves
first-party imports against the filesystem, and with `manifest/` absent it reclassified
`pii_erasure.manifest` as third-party and demanded a different import order. The true
cause (a package that does not exist in the repository) was three inferential steps from
the reported symptom. A defect that misreports itself costs more than one that fails
loudly, and no amount of reading the diff would have found it: the diff looked complete.

The new guard needed no synthetic mutation to prove it can fail — it was written while
the bug was still present, and went red on the real defect before the fix landed.

**Second-order note.** This is the first defect in the log that local `make check` could
not have caught by construction: the gate runs against the working tree, and the working
tree was correct. The class is "the repository differs from the machine", and the only
mechanism that sees it is one that asks git rather than the filesystem. Worth remembering
when adding future guards.

### 2026-07-26 · V7-1 — every participant Lambda died at cold start

The first defect found by real AWS. `make deploy-dev` succeeded, all three stacks reached
`CREATE_COMPLETE`, and `make conformance` failed on its first call.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V7-1 | **High** | **`pii_erasure/__init__.py` resolved its own version through `importlib.metadata` at import time**, so importing the package required the distribution to be pip-installed. `make package` stages the Lambda asset by *copying* `src/pii_erasure` — there is no `.dist-info` in the artifact — so both participants failed every invocation with `Runtime.ImportModuleError: No package metadata was found for agentic-pii-erasure`. 34 errored invocations, not one contract assertion ever reached. | **Fixed.** `__version__` is now resolved lazily via a module-level `__getattr__`, so the lookup happens on attribute access (the CLI, which is always installed) and never during import (every Lambda). Absent metadata returns `0+unknown` rather than a fabricated number. Backed by `tests/unit/test_lambda_asset_imports.py`. |

**Why every hermetic gate passed.** `make check` imports `pii_erasure` from the venv,
where an editable install puts the metadata exactly where the code expected it. `cdk
synth` asserts the asset *directory* exists but never imports from it. Nothing hermetic
had ever executed the bytes that get uploaded. The shape is V6-1's, one artifact further
along: **the gate tested the repository, not the thing built from it.** V6-1 was "the
repository differs from the machine"; this is "the artifact differs from the repository."

**The guard is two independent mechanisms**, matching how invariant 1 is enforced:

- an *executable* probe that copies the package to a scratch directory, rebuilds `sys.path`
  to exclude the repo's source roots, strips editable-install meta path finders, makes
  `importlib.metadata` report the distribution absent, and imports every participant
  handler. It reproduces the CloudWatch traceback verbatim — same exception, same file,
  same line — which is the standard for saying a hermetic test covers a deployed failure.
- a *structural* check parameterised over every module in the package, failing any
  module-level call into `importlib.metadata`. The probe only covers modules a handler
  reaches today; this one covers the modules a later milestone will put on the cold-start
  path.

Both were written before the fix and went red on the live bug; the fix was then reverted
to confirm both still fail with the corrected probe, since the first version of the probe
failed for the *wrong* reason (`sys.path = [asset]` drops the standard library, so it died
on `import importlib` rather than on the defect). **A test that fails for the wrong reason
is not evidence — it is a coincidence that happens to be the right colour.**

**Honest limit.** The probe supplies third-party dependencies from site-packages, so it
cannot see a dependency the asset fails to vendor. `make conformance` remains the only
gate for that, which is ADR-017's argument restated: a hermetic test proves what its
author already modelled.

### 2026-07-26 · V7-2 — the deployed gate graded code that was never deployed

Found immediately after V7-1, by the fix appearing not to work.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V7-2 | **High** | **Nothing compared the deployed Lambda code against the working tree.** After V7-1 was fixed and committed, `make conformance` was re-run and reported 16 failures with the *same* `Runtime.ImportModuleError` — because the stack was still running the previous build. The suite has no notion of which bytes it is grading, so "the fix is wrong" and "the fix was never deployed" are indistinguishable from its output. The costlier of the two readings is the one it invites, since it sends you back to correct code. | **Fixed.** `tests/conformance/conftest.py` adds a session-scoped preflight that compares each named Lambda's `Code.S3Key` — the asset hash CDK derives from the staging directory — between the locally synthesised template and the deployed one, and fails the session before a single verb is graded. `make conformance` now depends on `package synth` so both sides of the comparison are current. |

**No fingerprinting scheme of our own.** CDK already hashes the asset directory into
`Code.S3Key`, so the question "is the running code built from this working tree?" has an
exact answer available from one read-only `GetTemplate` call. Resources without an explicit
`FunctionName` are excluded: those are aws-cdk-lib's own custom-resource handlers, and they
legitimately differ when the CDK version moves.

**The false pass is the mechanism, not a hypothetical.** Comparing the hashes *before*
re-staging the asset reported agreement — `c4cec548…` on both sides — because `cdk.out`
had been synthesised from the same stale staging directory the stack was deployed from.
Re-running `make package` changed the local hash to `659cc3cf…` and the disagreement
appeared. A staleness check whose inputs are themselves stale confirms whatever it is
shown, which is why the Makefile prerequisites are load-bearing rather than convenience.

The guard needed no synthetic mutation: it was written while the stack was still stale and
fired on the real divergence, naming both hashes. It was then verified in the passing
direction too, because a check that cannot go green is no more use than one that cannot go
red.

**Third-order note.** V6-1, V7-1 and V7-2 are one family seen from three distances — the
repository differing from the machine, the artifact differing from the repository, and the
deployment differing from the artifact. Each was invisible to every gate that ran *earlier*
in the chain, and each reported itself as something else. When adding a control, the useful
question is not only "can this fail?" but **"which link in source → artifact → deployment
does it actually observe?"**

### 2026-07-26 · V8 — pre-M4 validation pass

Swept before building M4, because M4 implements the two participants these claims describe.
Structural checks clean: **0 broken relative links** across all markdown; no superseded
vocabulary (Step Functions, Fargate, CrewAI, Strands, OpenSearch) outside superseded ADRs,
rejected-alternative tables and this file; `make check` green; none of the paths M4 creates
are gitignored (checked with `git check-ignore` before writing a line, per V6-1).

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-1 | **Medium** | **The repo's worked example of residual honesty misdescribes its own residual.** Seven documents and three source files stated that SES retains "the suppression hash". The SES v2 API has no hash anywhere: `PutSuppressedDestination` requires a plaintext `EmailAddress`, and `GetSuppressedDestination` returns `EmailAddress` as a required string. The real residual is the subject's **plaintext email address**, held at account level. | **Fixed** in all ten sites: the retained item is the *suppression entry*. The participant records that residual by digest — invariant 5 forbids the address itself appearing in a locator, ledger entry or log — and the docs now distinguish the two: what SES keeps, and how we are permitted to refer to it. |
| V8-2 | **Low** | **`GetVectors` caps at 100 keys, not 500.** The docs consistently state ≤500 per call, which is correct for `PutVectors` and `DeleteVectors` but wrong for `GetVectors` — verified against the botocore service model (`GetVectorsInputList max=100`). `vector-index.verify()` reads by key, so a verifier batching at the documented 500 would fail at runtime against a subject with a large corpus. | **Fixed.** ROADMAP now states both limits, and the participant batches each call at its own ceiling. |

**V8-1 is the interesting one, because the error was in the direction of comfort.** A hash
is a *less* sensitive residual than a plaintext address. The file whose entire purpose is
"disclose what remains, never hide it" understated what remains — not by lying, but by
inheriting a plausible detail nobody had checked against the service. That is the same
mechanism as V5-1 (the O(1) tool-surface claim): a statement about an AWS service that
reads as technical fact, was written from a mental model of how such a service *would*
work, and had never been compared with the API.

The check that found it is cheap and worth repeating whenever a doc describes a service's
behaviour: **read the botocore service model, not the prose.** Required members and shape
constraints are machine-readable, local, and versioned with the SDK actually installed.

### 2026-07-26 · V8-3 / V8-4 — found by building M4, not by reading it

Two more from the same pass, both surfaced by implementation rather than review. Recorded
separately because their discovery mode is different: V8-1 and V8-2 came from reading the
API model *before* writing code; these came from the code refusing to be written.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-3 | **High** | **The conformance suite contained two assertions that cannot both hold.** `test_residual_honesty` requires `analytics-lake` and `notify-suppression` to return `PARTIAL` with a non-empty residual; `test_verify_is_clean_only_after_hard_delete` required `clean is True` for *every* participant; and `VerifyResponse` forbids `clean=True` alongside anything remaining. Satisfying the suite would have required those two participants to claim an erasure they had not performed — the precise dishonesty invariant 7 exists to forbid, demanded by the suite that enforces invariant 7. It was green only because both participants were unbuilt and skipped. | **Fixed, and made stricter.** The assertion is now archetype-aware: residual-by-design participants must report `clean=False`, and their `remaining` set must **equal** the residual their `hard_delete` disclosed. That cross-checks two verbs against each other, so a participant that discloses a residual and then forgets it in `verify` now fails — a stronger claim than the one it replaced. |
| V8-4 | **Medium** | **"No VPC" was false in seven places.** `README.md`, `ARCHITECTURE.md` (×3), `PROJECT-STRUCTURE.md`, `CLAUDE.md` (×2) and ADR-016 asserted the platform uses no VPC. Aurora Serverless v2 cannot exist outside one — a cluster needs a DB subnet group, which needs subnets. The claim was written when the participants were simulated (ADR-012), where it was true, and survived the rewrite to real services (ADR-017) without being re-checked against them. | **Fixed** in all seven, plus [ADR-023](adr/ADR-023-aurora-needs-a-vpc.md). The enforceable property — *nothing we run attaches to a VPC* — is unchanged and still asserted at synth time. The VPC holds the cluster and nothing else: isolated subnets, no NAT, no IGW, no endpoints, so it bills nothing. A new synth assertion fails if any of those appear. |

**V8-3 is the most interesting defect in this log so far**, because the suite was not
merely wrong — it was wrong *in the direction of the thing it existed to prevent*. A gate
that cannot pass without lying is worse than a missing gate, since it applies pressure
toward the lie. It also could not have been caught by running the suite: skipping is the
correct behaviour for an unbuilt participant, so the contradiction was invisible until the
participant existed. **The only way to find it was to try to satisfy it.**

**V8-4's mechanism is now familiar**: a claim that was true of an earlier architecture,
carried across a rewrite because rewrites update code and rarely re-audit prose. Same
shape as V5-1 and V8-1. The generalisable check is *when an ADR supersedes another, the
claims that depended on the superseded decision need re-reading* — ADR-017 replaced
simulated participants with real ones, and "no VPC" was a property of the simulation.

**A smaller one worth naming**, caught by a test written during this milestone rather than
after it: `erasure seed` read a CloudFormation output called `DekRegistryTableName`, which
does not exist — the foundation stack exports `DekRegistryTable`. That is a `KeyError`
reachable only by a human running `make seed` against a deployed stack, i.e. the slowest
and most expensive feedback loop available. It is now asserted hermetically: every output
key the CLI reads is checked against the synthesised templates.

### 2026-07-26 · V8-5 — a pinned engine version that does not exist in the region

Found by `make deploy-dev` failing, ten minutes in.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-5 | **Medium** | **`AuroraPostgresEngineVersion.VER_16_6` synthesised cleanly and CloudFormation rejected it**: `Cannot find version 16.6 for aurora-postgresql`. In `us-west-2`, 16.6 exists only as `16.6-limitless` — a different engine mode, not the standard version. The CDK enum is a compile-time list of versions that have existed *somewhere* when the library was published; it is not a statement about what a given region offers today. `cdk synth` therefore validated the shape of a cluster that cannot be created. | **Fixed** to `VER_16_13`, confirmed in-region and confirmed to report `ServerlessV2FeaturesSupport.MinCapacity = 0`, which `serverless_v2_min_capacity=0` depends on. New `make preflight` target asks RDS before deploying; `deploy-dev` now depends on it. Verified in both directions — it fails on `VER_16_6` and passes on `VER_16_13`. |

**This is a genuine limit of the hermetic gate, not an oversight in it.** `make check`
runs without an AWS account by design, so it cannot know which engine versions a region
offers, which instance classes are available in an AZ, or which service quotas apply. Those
facts are only knowable by asking AWS. The useful response is not to weaken the hermetic
gate's independence but to add a *third*, cheap category between the two: a read-only,
free, credentialed check that runs before anything is created.

`make preflight` is that category's first member. It costs one API call and seconds; the
failure it replaces cost a full create-and-rollback cycle. Candidates to add as they bite:
instance-class availability per AZ, service quotas, and whether S3 Vectors is offered in
the target region at all.

**A smaller lesson inside the fix.** The first version of the check extracted the engine
version with `grep -oE 'VER_16_[0-9]+'`, which matched the *comment above the declaration*
— a comment naming the version that had just failed. It duly reported `16.6` unavailable:
a correct answer to the wrong question, and a check that would have kept failing after the
bug was fixed. The pattern is now anchored to the attribute access. Guards need testing in
both directions for the same reason the things they guard do.

### 2026-07-26 · V8-6 — the Makefile and the CLI disagreed about the CLI

`make seed` failed with `No such option: --tenant`.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-6 | **Medium** | **Seven `make` targets invoked CLI commands with options those commands do not accept.** `seed` was rewritten at M4 and its make target was not re-read, so the two drifted apart *in the same commit that built them*. The other six (`ledger --verify`, `discover --subject`, `threads --list`, `resume --thread`, `approve --thread --decision`, and `inspect --participant`) had drifted earlier and silently: click rejects unknown options *before* the command body runs, so an unbuilt command printed `No such option` instead of "⏳ lands at M5" — a parser error standing in for a roadmap fact, and the M0 design intent quietly lost. | **Fixed.** `seed` takes `--tenant`; unbuilt commands accept unknown options so they can still announce their milestone; `inspect` was rewritten to the interface the Makefile and ROADMAP already documented. Backed by `tests/unit/test_makefile_cli_contract.py`, which introspects the click parser rather than a hand-maintained flag list. |

**Two second-order findings came out of it, both worse than the reported error.**

**`inspect` had drifted in *meaning*, not just in flags.** The Makefile and ROADMAP both
say "dump one participant's state", and I built it to take a subject handle and print that
subject's placement across systems. Same word, different command. The flag mismatch was
the visible symptom of a semantic divergence that a flag-name check alone would have
"fixed" by changing the Makefile to match the wrong implementation. It now matches its
documented interface: `--participant` required, `--subject` optional as a filter.

**The tenant had two sources of truth.** `--tenant` came from `PII_ERASURE_TENANT` and
`_stack_config()` *also* read that variable, while `seeds/meridian.json` declares
`tenantId` independently. Nothing reconciled them, so a divergent env var would have
stamped one tenant's name onto another tenant's seeded rows — wrong in every downstream
count, with no error to notice. The flag is now an **assertion**: it is compared against
the seed file and a mismatch stops the run. This is V4-4's lesson one turn further on — a
setting nothing reads is bad; a setting that silently *wins* over the fixture is worse.

**Why no gate caught it.** `make check` runs the test suite; it never runs the make
targets. The CLI's own tests call functions directly, so they never traverse the parser.
The seam between "how the Makefile invokes the CLI" and "what the CLI accepts" had nothing
watching it, and it is a seam that only reports at the worst moment — `seed` is a
deployed-gate target, so the failure surfaces after a human has stood up a stack and is
waiting on it.

The new test closes it by introspecting the click command objects Typer builds, so it
cannot drift from the real parser the way a list of expected flag names would. Its
exemption for unbuilt commands is itself guarded: a *built* command carrying
`ignore_unknown_options` would accept `--typo` in silence, so a second test fails if the
exemption is ever used outside `_UNBUILT`. Both verified by mutation.

### 2026-07-26 · V8-7 — the 0-ACU floor has a runtime consequence nobody had handled

`make seed` failed with `DatabaseResumingException`.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-7 | **High** | **Nothing handled Aurora's auto-pause resume.** `serverless_v2_min_capacity = 0` means the cluster pauses when idle, and the first statement afterwards fails with `DatabaseResumingException` while it wakes. The API models this as a **400 with `retryable` unset**, so botocore's default retry policy does not touch it. Both the seeder and the `billing-ledger` participant would have failed on the first call after any idle period — and in a saga that means an erasure failing because the database was asleep. Phase 3 does not compensate (invariant 6), so a spurious failure there is expensive to unwind. | **Fixed.** `execute_with_resume()` waits out the resume with a bounded budget (120s) and fails loudly past it. Used by the participant *and* the generator, so the behaviour has one definition. `billing-ledger`'s Lambda timeout raised to 180s, since a function killed mid-wait converts a pause into a failure. |

**This is the cost rule's bill arriving.** [ADR-021](adr/ADR-021-s3-vectors-for-cost.md)
says nothing may bill continuously for existing, and `min_capacity = 0` is how the
relational archetype satisfies it. The trade was documented as "cold-resume latency" — a
performance note. It is not: it is a **distinct error code on the first call**, which is a
correctness concern, and the doc's framing hid that. The synth assertion proving
`MinCapacity: 0` was green throughout; it asserts the setting exists, and could not assert
that anything copes with what the setting causes.

**Only the resume error is retried.** `DatabaseUnavailableException` and the rest
propagate. "The cluster is waking up" is a known, self-clearing state with a documented
end; "the cluster is unavailable" is not, and silently retrying a delete against an
unhealthy database is a different risk that deserves a different answer rather than being
folded into the same loop.

**A useful thing the failure confirmed.** Reaching `DatabaseResumingException` proves the
statement got to the cluster, which settles the open question from V8-5: **the RDS Data API
does work on Aurora PostgreSQL 16.13.** `describe-db-engine-versions` returns null for
`SupportsHttpEndpoint` on this engine family, so it could not be confirmed in advance —
recorded here rather than left as an open uncertainty.

**Pattern across V8-5, V8-6 and V8-7.** All three were found by *running* the thing, and
none was reachable by any hermetic gate: regional version availability, the Makefile→CLI
seam, and a service's cold-start error taxonomy are all facts about the world rather than
about this repository. The hermetic gate remains the right shape; the honest conclusion is
that the deployed gates are load-bearing rather than confirmatory, and running them earlier
is worth more than making `make check` stricter.

### 2026-07-26 · V8-8 — the seeder was not re-runnable, and two writers failed silently

`make seed` failed with `UsernameExistsException` on a re-run after V8-7's partial run.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-8 | **High** | **Four of the eight writers were not idempotent, and the reported error was the least serious.** `AdminCreateUser` and `CreateContact` raise on a second run — loud and self-announcing. `PutObject` against the **versioned** upload bucket adds a version rather than replacing one, and an Iceberg `INSERT` appends: after two runs the map would claim `objects=3` while the bucket held six, and the recall gate's denominator would be wrong **with nothing raising**. A generator that inflates what it measures against is worse than one that crashes. | **Fixed.** Every writer now converges. `upload-bucket` purges the subject's versions first, `analytics-lake` deletes the subject's rows first, `compliance-archive` writes only what is missing, and the two identity writers treat "already exists" as the declared state reached. Backed by `tests/unit/test_seed_idempotency.py` — run twice, assert identical map *and* identical underlying counts. |

**The strategies differ by archetype, and that is the interesting part.** Purging is
correct for `upload-bucket`, and **impossible** for `compliance-archive`: COMPLIANCE-mode
Object Lock refuses deletion from everyone including root, so the seeder must write only
what is absent. The archetype asserts itself on the fixture generator exactly as it does on
the participant — the same lesson arriving from the other side. A generic "delete then
re-create" seeder would work for seven participants and be unimplementable for the eighth.

**`make seed` is re-run constantly** — after a partial failure, before an eval, when a
subject is added — so this was never a corner case. It became visible only because V8-7
left a partial run behind, which is a reminder that a failure's *residue* is part of its
cost.

**Two bugs in the test, both worth recording**, because they are the same class as the
defects they were written to catch:

* The fake modelled Object Lock **per client**, so the legitimate `upload-bucket` purge
  looked like a violation. Object Lock is a property of the *bucket*; one boto3 client
  serves both. A fake that disagrees with the service about where a control lives will
  fail honest code and pass dishonest code.
* The fake then ignored the `Bucket` parameter entirely, so both buckets shared one object
  list — and since both use `subjectRef/` as a prefix, the archive's existence probe saw
  the upload bucket's objects and concluded it had nothing to write. The test failed for a
  reason that had nothing to do with the code under test.

Both were caught by the tests failing in ways the described defect could not explain, which
is the same signal that found the wrong-reason probe in V7-1. **"Red for a reason I cannot
account for" is worth as much attention as red itself.**

### 2026-07-26 · V8-9 — the relational participant had no schema

`make seed` failed with `relation "public.customers" does not exist`.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-9 | **High** | **Nothing ever created the billing ledger's tables.** `handler.py` names `public.customers`, `public.invoices`, `public.invoice_lines` and `public.legal_holds` in fixed statements; the stack creates a cluster and a database and stops there. Every unit test passed because the Data API client was faked — a fake answers the question it was given and has no opinion about whether the table exists. The participant, the conformance suite and the saga would all have failed the same way. | **Fixed.** `billing_ledger/schema.py` holds the DDL beside the SQL that depends on it, applied idempotently by the generator on first use. Backed by `tests/unit/test_billing_schema.py`: every table and column the handler names must exist in the schema, delete order must be the reverse of create order, and the foreign keys must be `RESTRICT`. |

**The DDL lives in `src/`, not `infra/`.** CloudFormation cannot create a table inside a
database; doing it "in the stack" would mean a custom resource that runs this same SQL.
Keeping it beside the queries means table names, column names and delete ordering are one
file apart and comparable by a unit test, which is what closes the gap for good.

**`ON DELETE RESTRICT`, and it is load-bearing.** `CASCADE` would make
`DELETE FROM public.customers` silently remove the invoices and lines too. It would *work* —
and it would delete the entire point of the RELATIONAL archetype, because referential
integrity would no longer dictate anything, `_DELETE_ORDER` would become decorative, and a
reader would take away the opposite of the intended lesson. RESTRICT makes the database
refuse a wrong-order delete, which is what makes the ordering demonstrable rather than
asserted. There is now a test that fails if anyone "fixes" the constraint.

**Why no fake could have caught this.** The unit tests inject a fake Data API client, and a
fake has no schema — it answers whatever it is asked. That is not a flaw in the fakes; it
is the boundary of what a fake can tell you, and the reason ADR-017 makes the deployed gate
the real one. The generalisable form: **a test double can verify the shape of a call, never
the existence of the thing it addresses.** Every participant reaching a store with structure
— tables, indexes, a vector index, a Glue table — needs that structure created somewhere,
and the hermetic gate can only check that the two definitions agree, never that either is
real.

**The same gap existed one participant along, and was closed in the same commit.**
`analytics-lake` queried a Glue table nothing created; it had not failed only because
`make seed` stops at Aurora first. `analytics_lake/schema.py` now creates it as **Iceberg** —
required, not stylistic: `UPDATE` and `DELETE` are unavailable on a plain external table, so
three of the five verbs would have been impossible while `discover` and `verify` kept
working, leaving the participant looking half-alive rather than broken. The table's
`vacuum_max_snapshot_age_seconds` is derived from the same constant the participant
discloses in its `PARTIAL` residual, and a test asserts they agree — a retention window an
approver is shown must be one the table actually honours.

**A postscript on the guard itself.** The new check extracts the config keys the generator
reads and compares them with what the CLI supplies. Its first version matched only
double-quoted subscripts and silently missed `self._config['analyticsBucket']` — single-quoted
because it sits inside an f-string. It read 15 of 16 keys and reported success, ignoring the
newest one: precisely the failure it was written to prevent, one level up. Caught by
mutation-testing it and noticing the mutation produced *no* failure. **When a mutation
changes nothing, check that the mutation applied before concluding the code is fine.**

### 2026-07-26 · V8-10 — the Iceberg DDL was written in the wrong dialect

`make seed` reached Athena and failed with
`mismatched input 'LOCATION'. Expecting: 'COMMENT', 'WITH'`.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-10 | **Medium** | **`CREATE TABLE IF NOT EXISTS` is not in Athena's Iceberg grammar.** Adding the clause routes the statement into Athena's generic (Trino) `CREATE TABLE` parser, which expects table properties in a `WITH (...)` clause and rejects `LOCATION` outright. The `LOCATION` + `TBLPROPERTIES` form I had written is correct — for a statement *without* `IF NOT EXISTS`. The error names `LOCATION`, which is the one part that was right. | **Fixed** against the Athena user guide rather than memory. Idempotency now comes from catching "already exists", the same shape used for Cognito and SES. `EXTERNAL` is also asserted absent — it fails with *External keyword not supported for table type ICEBERG*. |

**Second defect in the same call path, and the more dangerous one.** The generator's Athena
runner waited for state `SUCCEEDED` and nothing else, so a **failed** statement span the
full 30-second consistency budget and then raised a *timeout*. Athena's actual complaint —
sitting in `StateChangeReason` the whole time — was discarded, and the report blamed
slowness. It now waits for any terminal state and raises with the service's own reason.

That is the third time in this log a defect has arrived wearing the wrong label: V6-1
reported a missing package as an import-sort error, V7-2 reported a stale deployment as a
code failure, and this reported a syntax error as a timeout. **A control that mislabels a
failure costs more than one that misses it**, because it spends the reader's attention in
the wrong place — and unlike a miss, it does so confidently.

**On guessing at syntax.** The recalled form was the Hive `CREATE EXTERNAL TABLE` shape,
which is right for a Glue external table and wrong for Iceberg. ROADMAP rule 3 says verify
AWS API shapes against current documentation rather than memory; I applied that to the
*botocore models* (V8-1, V8-2) and not to *SQL dialects*, which are equally versioned,
equally service-specific, and not covered by any model I can introspect locally. Both
guards are mutation-tested: reinstating `IF NOT EXISTS` fails, and widening the
already-exists catch to swallow everything fails too — the latter matters because a broad
catch would turn a genuine DDL error into a silent no-op, which is V8-9 again, self-inflicted.

### 2026-07-26 · V8-11 — the residual archetype needs an account capability nobody checked

`make seed` reached SES and failed with `Your account is still in the sandbox.`

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-11 | **Medium** | **`PutSuppressedDestination` is refused for accounts without SES production access**, which every new AWS account lacks. `notify-suppression` is invariant 7's worked example: with no suppression entry it has nothing to retain, so `hard_delete` returns `APPLIED` rather than `PARTIAL` and the RESIDUAL_BY_DESIGN archetype is not demonstrated at all. Nothing in the repo named this as a prerequisite. | **Fixed three ways.** `make preflight` reports sandbox status up front (`GetAccount.ProductionAccessEnabled`, a boolean). `make seed` **fails by default** with the remedy. `make seed ALLOW_SES_SANDBOX=1` proceeds and writes a `degraded` block into the ground-truth map naming exactly what is missing. |

**Why not simply skip it.** Silently omitting the entry would leave a seed that looks
complete, a `notify-suppression` that returns `APPLIED`, and a reader concluding the
residual archetype works — while the one artifact it exists to demonstrate was never
created. That is a worse outcome than a hard failure, and it is the same shape as baseline
finding #4: a fixture that cannot fail because the thing it grades was quietly removed.

**Why not simply block.** Production access is an AWS support request that can take a day.
Refusing to seed seven working participants because the eighth needs a capability the
account does not yet have would be disproportionate.

So the degradation is **explicit, opt-in, and recorded in the artifact itself** rather than
only in a log line the operator may not have kept. A later reader of `ground-truth.json`
sees the gap without having to remember the run. Both properties are mutation-tested:
removing the `degraded` record fails, and widening the sandbox match to swallow any
`BadRequestException` fails too — the match is on the message, because SES reports the
sandbox through a generic error code, and a broad catch would absorb real defects.

**The pattern this completes.** `make preflight` now checks two things the hermetic gate
cannot know — a regional engine version (V8-5) and an account capability (V8-11). Both are
facts about *the environment*, not about the repository, and both previously surfaced only
after a slow, expensive operation had begun. That is the category the preflight target
exists for, and it is worth extending whenever a deployed gate fails for a reason
`make check` could never have held an opinion about.

### 2026-07-26 · V8-12 / V8-13 — validating the first real seed

`make seed ALLOW_SES_SANDBOX=1` succeeded. Cross-checking the emitted map against the
services found one defect in the map itself and one in the conformance suite.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| V8-12 | **High** | **The ground-truth map echoed the declaration instead of measuring the write.** Every `_write_*` returned the numbers it was handed. For `upload-bucket` those differ from reality: seeding `objects=3, deleteMarkers=1` writes **four** object versions, because the tombstoned object — the one whose marker demonstrates that a marker is not a deletion — is itself a version, and `discover` counts versions. Confirmed by invoking the deployed participant: `discover` reports `object=4`, the map said `3`. The gap would have surfaced as a recall miss charged to the discovery agent. | **Fixed.** Every writer now reads back what the service holds — version counts, a `KeyCount`, `GetVectors` probes, a `SELECT count(*)`, a consistent `Query` — and the module docstring's claim that each writer "returns what it actually created" is now true rather than aspirational. |
| V8-13 | **Medium** | **The conformance suite leaves its throwaway subjects behind**, contradicting its own docstring ("leaves nothing behind"). Four runs deposited 22 `sub_conf_*` subjects in the upload bucket, 24 in the compliance archive and 23 in the DEK registry. Only the tests that call `hard_delete` clean up; `discover`, `soft_delete`/`restore`, idempotency and evidence tests do not. In the **Object Lock** bucket the residue cannot be removed until retention expires. | **Open**, recorded here. The seeded objects are fabricated and harmless, but they inflate every by-prefix count in the account and make manual inspection noisy. Fix belongs with the conformance seeder work still outstanding for M4. |

**V8-12 is the most consequential defect in this log**, because of what it would have
looked like. `make eval` compares discovery's output against the map; the map was wrong by
one; recall would have come out below 1.0; the gate would have gone red, and **the obvious
suspect would have been the discovery agent** — the one component that was behaving
correctly. Hours would have gone into prompt tuning to fix a counting bug in the fixture.
That is baseline finding #4 inverted: not a fixture that cannot fail, but a fixture that
fails the wrong component.

It also shows how a docstring can insulate a defect. The module said, in its opening
paragraph, that the map is assembled from what each writer "actually created" — and I read
that sentence several times while writing V8-8's idempotency fixes without checking whether
the returns were measurements or echoes. **A claim written by the same author, in the same
file, is not evidence.** The check that found it was mechanical and took one command:
invoke the participant, compare with the map.

**Method note.** Everything else reconciled exactly — Cognito 5/5, profile items 3/3/4/2/2,
S3 Vectors 12/9/6, the SES contact, the archive's two locked objects and its DEK. The
discrepancy was visible only because the raw S3 version listing was compared against the
map rather than the map against itself. Validating an artifact against the system it
describes is worth more than validating it for internal consistency, which it always has.

### 2026-07-26 · M4's second half: conformance covers all eight, and V8-13 closes

The conformance suite could seed only two of the eight registered participants; the other
six skipped with "no conformance seeder yet". Structurally that was V8-3's shape aimed at
the deployed gate itself: `make conformance` would have reported 16 passed / 48 skipped
and read as green while proving nothing about six participants.

**Seeding now reuses the ground-truth generator's writers** — the code path `make seed`
already proved against the deployed services, measuring what it writes (V8-12). A bespoke
conformance seeder would have been a second implementation free to drift from the one the
recall gate trusts.

**V8-13 closes.** Every test's throwaway subject is torn down in fixture teardown, by
direct AWS calls rather than the participant's own verbs — cleanup must not depend on the
behaviour the test just judged. The claim is scoped honestly: Object Lock ciphertext is
undeletable by anyone until the dev retention window expires (the WORM archetype asserting
itself on its own test rig), and the idempotency log keeps pseudonymous receipts. Everything
addressable is removed, including on the sandbox skip path, where the already-created SES
contact is cleaned before skipping.

**The seam is now guarded hermetically.** `tests/unit/test_conformance_coverage.py`
asserts PLACEMENTS and `_cleanup` cover the registry exactly, that the "no seeder yet"
escape hatch stays deleted, and that the subject fixture tears down after `yield`.
Mutation-tested: dropping a placement fails, and reverting the fixture to a plain return
fails. Registering participant #9 without conformance coverage now breaks `make check`
instead of silently skipping the one suite that costs money to run.

**One capability gate remains, by design**: in an SES-sandbox account, notify-suppression's
tests skip with a reason naming the fix (production access) — a gate that goes mandatory
the moment the capability exists, not a silencing guard. Expected shape: 56 passed /
8 skipped in the sandbox; 64 passed / 0 skipped once production access lands.

### 2026-07-27 · M5's deployed gate, and the control that cried wolf

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| **V9-1** | **High** | The saga Lambda asset was **non-deterministic**, so `make integration` failed its own V7-2 staleness preflight on a stack that had just been deployed successfully. `pip install --target` materialises entry-point wrappers into `bin/` even under `--platform`: Windows `.exe` launchers are a zip with a stub prepended, and the embedded zip timestamp changes the bytes on **every build**; POSIX console scripts embed the **build machine's** interpreter path. Ten files (5 wrappers + the 5 `RECORD`s listing them) changed per rebuild, so the CDK asset hash identified the *build*, not the *source*. | `make package` strips `bin/` from both assets and filters the stripped entries out of `dist-info/RECORD`. Verified by measurement: two full package+synth cycles now produce byte-identical asset hashes (`d576d6ad…` twice), where before they differed (`12fe207d…` deployed vs `0c6d9e43…` rebuilt). Guarded by `tests/unit/test_lambda_asset_determinism.py`. |

**This is V7-2's mirror image, and that is the interesting part.** V7-2 was a control that
stayed *silent* while the deployed bytes drifted from the working tree — it missed a real
failure. V9-1 is the same control *firing on a difference that was not a source change at
all*. Both produce the same class of damage: the operator is sent to investigate the wrong
thing. A gate that cannot pass is not a strict gate, it is a broken one, and the temptation
it creates — exempt the saga stack from the preflight — would have quietly restored V7-2 for
the one stack whose staleness is most expensive to miss.

**The load-bearing distinction: an artifact must be a function of its source.** The asset
hash is the right fingerprint precisely because CDK derives it from content; that only holds
if the content is derived from the source and nothing else. Console-script wrappers made it
a function of the machine and the minute. Stripping them is not a workaround for the
checksum — they were never correct to ship: nothing in Lambda runs a console script, and
five Windows PE binaries were being uploaded to a Linux runtime.

**Why the participants asset never showed this.** It vendors only `pydantic` and
`structlog`, neither of which declares console scripts, so `bin/` was never created and the
conformance preflight has been honest all along. The defect appeared with the first
dependency set that had entry points — which is also why the guard covers *both* assets
rather than the one that broke.

**Cross-machine, not just cross-build.** The POSIX half of the same defect would have
struck CI even if the timestamps had been stable: a shebang naming the build machine's
interpreter makes the hash differ between a Windows developer and a Linux runner, so the
preflight would have failed for everyone whose machine did not build the deployment.
Stripping `bin/` removes both halves. Verified locally across rebuilds; the cross-platform
half is asserted structurally (no host-specific artifacts survive packaging) rather than
demonstrated, since this session had one platform available.

**Mutation-tested both ways**: restoring a `bin/httpx.exe` fails the staged-asset checks,
and removing the strip step from the Makefile fails the recipe check. Reverting each
restores green.

| ID | Severity | Finding | Resolution |
|---|---|---|---|
| **V9-2** | **Medium** | `FixtureGenerator._writers()` is a documented reuse seam — the conformance and integration suites both seed by calling individual writers, deliberately reusing the code path `make seed` proves rather than writing a second implementation. But the notify-suppression writer's **degraded branch** appended to `self._truth`, which only `run()` ever assigned. A writer that worked when reached through `run()` raised `AttributeError: 'FixtureGenerator' object has no attribute '_truth'` when called directly — and only on the SES-sandbox path, which is the environment the deployed gate actually runs in. Two of M5's four integration scenarios errored in fixture setup. | Degradations are now the generator's own state, initialised in `__init__` and copied into the `GroundTruth` by `run()`, via a `_record_degraded()` helper that also deduplicates (one missing capability is one gap, not one per subject). Guarded by two tests in `tests/unit/test_seed_idempotency.py` that call the writer **directly** on the sandbox path. |

**The shape: an object that was only valid halfway through its own lifecycle.** Nothing
about the writer's signature said "call `run()` first", and the happy path never needed it —
only the branch that fires when a capability is missing. That is the worst possible place
for a latent dependency, because it is the branch that runs least often in development and
most often in the environments people actually deploy into. The fix is not "document the
ordering"; it is to make the object valid from construction, so the seam is safe for the
next caller who has not read `run()`.

**Why the guard calls the writer directly.** The existing sandbox tests all drove
`generator.run(SEEDS)`, so every one of them passed while the direct-call path was broken —
the tests and the defect were on opposite sides of the same seam. Mutation-tested by
restoring the `self._truth` form: the two new direct-call tests fail with the same
`AttributeError` the deployed gate produced, and reverting restores green.

**No redeploy is needed to clear it.** `evals/` is test-side code and never enters a Lambda
asset, so the V7-2 staleness hashes are unchanged — confirmed by rebuilding and re-synthing
to the same `d576d6ad…`. A fix that required a redeploy to verify a *test harness* change
would be its own smell.

1. Read a doc claim as an adversary: *what would make this false, and could the
   named control detect it?*
2. If the control can't go red, that's a finding — record it here with the fix and
   the guard that now backs it.
3. Never close a finding by weakening the control. Make it able to fail, then pass it.
