"""Phase-3 entry: re-evaluate legal holds LIVE — never cached from phase 1 (§5.3).

A hold placed during the grace window is a compliance defect if the phase-1 answer is
trusted. So this node calls `subject.discover` on every manifest participant — a
read-only verb — and blocks on any hold it finds. Blocking here leaves the subject
soft-deleted, which is the safe parked state while the hold is resolved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.contract import DiscoverRequest, DiscoverResponse
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state
from pii_erasure.saga.state import STATUS_BLOCKED


def make_hold_recheck(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def hold_recheck(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        found: list[dict[str, Any]] = []
        for participant in manifest.participants:
            request = DiscoverRequest(subject_ref=manifest.subject_ref, saga_id=manifest.saga_id)
            body = deps.participants.call(
                participant.system_id, "discover", request.digested_body()
            )
            response = DiscoverResponse.model_validate(body)
            found.extend(hold.digested_body() for hold in response.holds)

        if found:
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="BLOCKED_BY_HOLD_AT_RECHECK",
                body={"holdIds": [str(h.get("holdId")) for h in found]},
            )
            return {"status": STATUS_BLOCKED, "holds": found}

        deps.ledger.append(saga_id=manifest.saga_id, event_type="HOLDS_RECHECKED_CLEAR", body={})
        return {"phase": 3}

    return hold_recheck
