"""Verification sweep at T+0: every participant must report clean, or an honestly
disclosed residual (invariant 7 — the registry names which participants may hold one).

Anything else remaining is a failed erasure: DLQ + halt, forward recovery only.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state, verify_all_participants
from pii_erasure.saga.state import STATUS_STUCK


def make_verify(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def verify(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        receipts, unexpected = verify_all_participants(deps, manifest, verify_prefix="verify")

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
