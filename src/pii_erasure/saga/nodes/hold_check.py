"""Hold check: a legal hold vetoes **its scope**, before anything mutates (ADR-027).

This node used to block the whole saga on any hold at all, and said so — *"partial-scope
erasure under a hold is a policy decision no default should make, and the safe default is
to stop"*, explicitly framed as an M5 default. ADR-027 makes the decision that comment was
waiting for, and it goes the other way: a hold over `public.invoices` gives no lawful
basis to retain the subject's uploads, so stopping everything **silently over-retains** —
an under-deletion with no error attached, which is the failure recall cannot see.

So the saga now halts only when a hold covers everything actionable. There is then nothing
to erase, and parking the subject soft-deleted is the correct state while the hold is
resolved.

**Nothing here filters the manifest.** It is immutable after signature (invariant 3), and
it does not need editing: each participant already refuses its own held scope and returns
`PARTIAL` with a populated `residual` (invariant 7). The only defect was this node vetoing
the run before that machinery could operate.

The holds are re-evaluated *live* at phase-3 entry (`hold_recheck`) — this plan-time
answer is never cached across the gap (§5.3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.contract.holds import partition, unmatched_scopes
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state
from pii_erasure.saga.state import STATUS_BLOCKED


def make_hold_check(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def hold_check(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)

        # Aggregate holds apply to every participant; participant holds to their own.
        aggregate = list(manifest.legal_holds)
        held_total = 0
        actionable_total = 0
        scoped: dict[str, list[str]] = {}

        stray: set[str] = set()
        for participant in manifest.participants:
            holds = aggregate + list(participant.holds)
            held, actionable = partition(participant.artifacts, holds)
            held_total += len(held)
            actionable_total += len(actionable)
            if held:
                scoped[participant.system_id] = sorted(str(h.hold_id) for h in holds)
            # A scope that lands on nothing is far more likely mis-drafted than a hold
            # over absent data — `scope: "all"` matches only locators starting with the
            # letters "all". Under a subject-wide veto that mistake was invisible because
            # any hold stopped everything; under ADR-027 it silently protects nothing, so
            # it gets recorded rather than inferred.
            stray.update(unmatched_scopes(holds, participant.artifacts))

        holds_body = [hold.digested_body() for hold in aggregate]
        for participant in manifest.participants:
            holds_body.extend(hold.digested_body() for hold in participant.holds)

        if not holds_body:
            return {"holds": []}

        if actionable_total == 0:
            # Everything is held. Nothing to erase, so stop — and this is now the ONLY
            # way a saga blocks on holds, which makes `blocked` mean something specific.
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="BLOCKED_BY_HOLD",
                body={"holdIds": [str(h.get("holdId")) for h in holds_body]},
            )
            return {"status": STATUS_BLOCKED, "holds": holds_body}

        # Partially held: proceed, and say exactly which holds applied where. An operator
        # who learned the old behaviour will read this run as a hold being ignored, so the
        # ledger has to carry the scoping rather than leave it inferable (ADR-027 cost 2).
        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="HOLDS_SCOPED",
            body={
                "holdIds": [str(h.get("holdId")) for h in holds_body],
                "heldArtifacts": held_total,
                "actionableArtifacts": actionable_total,
                "systems": sorted(scoped),
                "scopesMatchingNothing": sorted(stray),
            },
        )
        return {"holds": holds_body}

    return hold_check
