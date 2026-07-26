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

1. Read a doc claim as an adversary: *what would make this false, and could the
   named control detect it?*
2. If the control can't go red, that's a finding — record it here with the fix and
   the guard that now backs it.
3. Never close a finding by weakening the control. Make it able to fail, then pass it.
