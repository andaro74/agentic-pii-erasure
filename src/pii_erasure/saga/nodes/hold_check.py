"""Hold check: a legal hold vetoes erasure, before anything mutates.

Aggregate `legalHolds` on the manifest block outright; participant-level holds do too
at M5 — partial-scope erasure under a hold is a policy decision no default should
make, and the safe default is to stop. The holds are re-evaluated *live* at phase-3
entry (`hold_recheck`) — this plan-time answer is never cached across the gap (§5.3).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state
from pii_erasure.saga.state import STATUS_BLOCKED


def make_hold_check(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def hold_check(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        holds = [hold.digested_body() for hold in manifest.legal_holds]
        for participant in manifest.participants:
            holds.extend(hold.digested_body() for hold in participant.holds)

        if holds:
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="BLOCKED_BY_HOLD",
                body={"holdIds": [str(h.get("holdId")) for h in holds]},
            )
            return {"status": STATUS_BLOCKED, "holds": holds}
        return {"holds": []}

    return hold_check
