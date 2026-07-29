# ADR-027: A legal hold blocks its scope, not the subject

- **Status:** Accepted — supersedes the subject-wide veto in `saga/nodes/hold_check.py` and `hold_recheck.py`, which those files described as an M5 default pending this decision
- **Anchors invariants:** CLAUDE.md #7 (participants report residuals honestly), #8 (recall gates the build)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

M8's deployed gate ran the walkthrough against `sub_dmi_2b8e4406`, the litigation-hold fixture, and the saga halted with `BLOCKED_BY_HOLD` before phase 2. That is what the code does. It is not what the rest of the repo says it should do.

Three artifacts describe holds as **scoped**:

| Where | What it says |
|---|---|
| [`participants/_base/holds.py`](../../src/pii_erasure/participants/_base/holds.py) | *"A hold blocks a scope, not a subject… Treating it as subject-wide would silently under-delete, which is a recall failure wearing a compliance costume."* — and `blocks()` implements prefix matching, per artifact |
| `seeds/meridian.json` | *"Scoped to one table on purpose. The uploads and profile items are still erased; treating a scoped hold as subject-wide would silently under-delete."* |
| ARCHITECTURE §7.1 | `deletability: BLOCKED_BY_HOLD` is a field on the **participant's** discover response, not on the subject |

One describes it as **subject-wide** — `saga/nodes/hold_check.py`:

> *"Aggregate `legalHolds` on the manifest block outright; participant-level holds do too **at M5** — partial-scope erasure under a hold is a policy decision no default should make, and the safe default is to stop."*

That comment is honest about being provisional. It names its milestone and calls itself a default. This ADR is the decision it was waiting for.

## Decision

**A hold blocks the artifacts within its scope. The saga proceeds for everything else, and what was retained is disclosed as residual risk.**

The saga halts only when a hold covers **everything** actionable — there is then nothing to erase, and parking the subject soft-deleted is the correct state while the hold is resolved.

Concretely:

- `hold_check` computes held-versus-total across the manifest. All held → `STATUS_BLOCKED`, unchanged. Otherwise it records the holds and continues.
- `hold_recheck` does the same at phase-3 entry, against **live** discover responses (§5.3 — a hold can appear during the grace window).
- Nothing filters the signed manifest. It is immutable after signature (invariant 3), and it does not need editing: **the participant layer already refuses its held scope and returns `PARTIAL` with a populated `residual`**, which is invariant 7's existing mechanism. The saga's only defect was vetoing the whole run before that machinery could operate.

## Consequences

- **Positive — over-retention stops being the default.** GDPR Art. 17(3)(e) exempts what is needed to establish or defend legal claims. A hold on `public.invoices` gives no lawful basis to retain the subject's uploads, and refusing to erase them is itself a compliance failure — one with no error attached, which is why it survived this long. Under-deletion is the failure ADR-008's gate exists to catch, and this was a path to it that recall could not see.
- **Positive — the layers stop disagreeing.** The participant scopes, the manifest carries per-participant holds, the discover response is per-participant. The saga now reads the same way.
- **Positive — a hold appearing during the grace window degrades rather than halts.** Phase 3 proceeds for the participants the hold never named. This is safe in the direction that matters: the approver authorised deleting *more* than will now happen, and deleting **less** than approved needs no re-approval. The reverse would.
- **Cost 1 — spoliation risk moves onto the scope string.** If a hold's `scope` is drafted more narrowly than the authority intended, this design erases data the legal team believed was frozen; the old behaviour would not have. Mitigated by prefix matching (a scope covers everything beneath it) and by the approval view, which now shows an operator what is being retained *and* what is being deleted under a hold before anything irreversible runs. **Not fully mitigated**, and worth stating plainly: the scope string is now load-bearing in a way it was not.
- **Cost 2 — every participant becomes a residual-bearing one, for one saga.** This decision was first implemented in the two nodes that *evaluate* holds, and the two that run immediately after them still graded residue against `registry.expects_residual` — a flag for participants whose residue is **structural** (SES suppression, Iceberg snapshots). `billing-ledger` is not one of those, so the T+0 `verify` called a lawful Art. 17(3)(e) retention a failed erasure and halted the saga at `stuck`; at T+7 the sweep would have raised a `RESURRECTION_INCIDENT` for data that never left. `verify_all_participants` now also accepts residue **the participant disclosed in its own phase-3 receipt**, matched by locator and count — invariant 7 obliges the participant to disclose honestly, and this is the platform's half of that bargain. Recorded as [VALIDATION.md V12-2](../VALIDATION.md#2026-07-29--v12-2--adr-027-was-implemented-in-the-node-that-decides-and-not-in-the-two-that-follow), because the shape of the mistake — a rule applied where it was written and not to the class it describes — is the fifth of its kind here.
- **Cost 3 — "blocked" becomes rarer and more meaningful.** A saga that halts on holds now means *everything* is held. Operators who learned the old behaviour will read the new one as a hold being ignored, so `HOLDS_SCOPED` is written to the ledger naming which holds applied to which participants.

## Alternatives considered

- **Keep the subject-wide veto.** Rejected: it silently over-retains, contradicts three of the four places holds are described, and the one place that defends it calls itself an M5 default. It is also the more dangerous error — an under-deletion nobody is alerted to, versus an over-deletion that cannot happen here because the participant still refuses its held scope.
- **Make it configurable per tenant.** Rejected for now: a flag would mean two behaviours to reason about and two to test, and there is no evidence yet that a real deployment needs the other one. If one does, it supersedes this.
- **Filter the held participants out of the manifest at plan time.** Rejected: manifests are immutable after signature, and a manifest that omits a held system would make the approval view describe a smaller blast radius than the estate actually has. The approver should see the whole picture and the retention within it.

## References

- [ADR-008](ADR-008-recall-1.0-hard-gate.md) (under-deletion is the failure that matters) · [ADR-002](ADR-002-three-phase-split-recovery.md) (phase 3 never compensates)
- CLAUDE.md invariant 7 · ARCHITECTURE.md §5.3 (holds re-checked, never remembered), §7.1
- `docs/VALIDATION.md` V11-8 (how the disagreement surfaced)
