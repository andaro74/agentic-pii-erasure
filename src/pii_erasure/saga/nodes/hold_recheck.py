"""Phase-3 entry: re-evaluate legal holds LIVE — never cached from phase 1 (§5.3).

A hold blocks its scope, not the subject (ADR-027): a hold that appears during the
grace window stops phase 3 for the participants it covers and for no others.

A hold placed during the grace window is a compliance defect if the phase-1 answer is
trusted. So this node calls `subject.discover` on every manifest participant — a
read-only verb — and blocks on any hold it finds. Blocking here leaves the subject
soft-deleted, which is the safe parked state while the hold is resolved.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.contract import DiscoverRequest, DiscoverResponse
from pii_erasure.contract.holds import partition
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state
from pii_erasure.saga.state import STATUS_BLOCKED


def make_hold_recheck(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def hold_recheck(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        found: list[dict[str, Any]] = []
        actionable = 0
        for participant in manifest.participants:
            request = DiscoverRequest(subject_ref=manifest.subject_ref, saga_id=manifest.saga_id)
            body = deps.participants.call(
                participant.system_id, "discover", request.digested_body()
            )
            response = DiscoverResponse.model_validate(body)
            found.extend(hold.digested_body() for hold in response.holds)
            # What this participant can still act on, LIVE — the artifacts it reports now
            # minus the ones the holds it reports now cover. Read from the response rather
            # than the manifest because the point of this node is that the estate may have
            # changed since plan time.
            _, free = partition(response.artifacts, response.holds)
            actionable += len(free)

        if not found:
            deps.ledger.append(
                saga_id=manifest.saga_id, event_type="HOLDS_RECHECKED_CLEAR", body={}
            )
            return {"phase": 3}

        if actionable == 0:
            # Everything the plan named is now held. Nothing left to hard-delete, so stop
            # and leave the subject soft-deleted — the safe parked state (§5.3).
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="BLOCKED_BY_HOLD_AT_RECHECK",
                body={"holdIds": [str(h.get("holdId")) for h in found]},
            )
            return {"status": STATUS_BLOCKED, "holds": found}

        # A hold appeared during the grace window covering only part of the plan
        # (ADR-027). Phase 3 proceeds for the participants it never named, and each held
        # participant refuses its own scope and returns PARTIAL + residual.
        #
        # Safe in the direction that matters: the approver authorised deleting MORE than
        # will now happen, and deleting LESS than approved needs no re-approval. The
        # reverse would — that is invariant 3, and it is not in play here.
        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="HOLDS_SCOPED_AT_RECHECK",
            body={
                "holdIds": [str(h.get("holdId")) for h in found],
                "actionableArtifacts": actionable,
            },
        )
        return {"phase": 3, "holds": found}

    return hold_recheck
