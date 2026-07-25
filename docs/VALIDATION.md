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

## How to run a pass

1. Read a doc claim as an adversary: *what would make this false, and could the
   named control detect it?*
2. If the control can't go red, that's a finding — record it here with the fix and
   the guard that now backs it.
3. Never close a finding by weakening the control. Make it able to fail, then pass it.
