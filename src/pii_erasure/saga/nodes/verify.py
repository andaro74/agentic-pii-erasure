"""Verification sweep at T+0: every participant must report clean, or an honestly
disclosed residual (invariant 7 — the registry names which participants may hold one).

Anything else remaining is a failed erasure: DLQ + halt, forward recovery only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.observability.metrics import emit
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import (
    emit_elapsed,
    manifest_from_state,
    verify_all_participants,
)
from pii_erasure.saga.state import STATUS_STUCK


def make_verify(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def verify(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        receipts, unexpected = verify_all_participants(
            deps,
            manifest,
            verify_prefix="verify",
            # Graded against what phase 3 disclosed, so a residual a participant named
            # in its own receipt — a scoped legal hold, ADR-027 — is not read as a
            # failed erasure by the node that runs immediately after it.
            receipts_so_far=state.get("receipts"),
        )

        # Emitted on every pass, including the clean one: a counter that is only written
        # when it is non-zero has no baseline, so "no data" and "nothing left behind" look
        # identical on a dashboard and the alarm cannot distinguish them.
        emit(
            "deletion.residual_artifacts",
            sum(int(item["remainingCount"]) for item in unexpected),
            deps.metric_dimensions("saga"),
        )

        # The statutory clock stops HERE, and this is the only node it could stop at.
        # `sweep` is where `status` becomes `completed`, but it gets there after the T+30
        # re-verification — a month *past* the answer the subject is owed. A duration
        # measured at the graph's END would therefore exceed any deadline-shaped
        # threshold on every healthy saga, which is an alarm nobody can leave switched
        # on. Art. 12(3) asks when the request was answered, and T+0 verification is
        # when it was.
        emit_elapsed(deps, state, "saga.duration")

        if unexpected:
            deps.dead_letters.send(
                {
                    "sagaId": manifest.saga_id,
                    "phase": 3,
                    "operation": "verify",
                    "unexpected": unexpected,
                }
            )
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="VERIFY_FOUND_RESIDUE",
                body={"unexpected": unexpected},
            )
            return {
                "status": STATUS_STUCK,
                "receipts": receipts,
                "errors": [{"node": "verify", "error": "unexpected_residue"}],
            }

        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="VERIFIED_CLEAN",
            body={"participantCount": len(manifest.participants)},
        )
        return {"receipts": receipts}

    return verify
