"""Compensation — phase 2's backward recovery, and phase 2's ONLY (invariant 6).

Restores what soft_delete disabled, in reverse phase-2 order, using the restore
tokens collected when the soft deletes ran. Reachable from: a phase-2 participant
failure, an approval denial or timeout, an invalid approval resume, and a subject
revocation during the grace window. **Unreachable from any phase-3 node** — the edge
path maps route phase 3 only forward, and a test asserts both that routing and this
module's absence from phase-3 sources.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.contract import MutationResponse, RestoreRequest, Verb
from pii_erasure.contract.idempotency import idempotency_key
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.invoker import ParticipantCallError
from pii_erasure.saga.nodes._shared import digest_from_state, manifest_from_state, receipt_key
from pii_erasure.saga.ordering import execution_order
from pii_erasure.saga.state import STATUS_COMPENSATED

STATUS_COMPENSATION_FAILED = "compensation_failed"


def make_compensate(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def compensate(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        digest = digest_from_state(state)
        restore_tokens = dict(state.get("restore_tokens") or {})
        existing = dict(state.get("receipts") or {})
        trigger = str(state.get("status", "unknown"))

        receipts: dict[str, Any] = {}
        compensated: list[str] = []
        failures: list[dict[str, Any]] = []

        # Reverse order: the last thing soft-deleted is the first thing restored, so
        # identity (revoked first) comes back last — after the systems it writes to.
        for participant in reversed(execution_order(manifest, phase=2)):
            token = restore_tokens.get(participant.system_id)
            if token is None:
                continue  # this system was never soft-deleted; nothing to undo
            key = receipt_key("restore", participant.system_id)
            if key in existing:
                compensated.append(participant.system_id)
                continue

            request = RestoreRequest(
                subject_ref=manifest.subject_ref,
                saga_id=manifest.saga_id,
                manifest_digest=digest,
                idempotency_key=idempotency_key(
                    saga_id=manifest.saga_id,
                    system_id=participant.system_id,
                    operation=Verb.RESTORE,
                    artifacts=participant.artifacts,
                ),
                artifacts=participant.artifacts,
                restore_token=str(token),
            )
            try:
                body = deps.participants.call(
                    participant.system_id, "restore", request.digested_body()
                )
                MutationResponse.model_validate(body)
            except (ParticipantCallError, ValueError) as error:
                failures.append(
                    {
                        "node": "compensate",
                        "systemId": participant.system_id,
                        "error": type(error).__name__,
                    }
                )
                continue  # keep restoring the rest — a partial restore beats none

            receipts[key] = body
            compensated.append(participant.system_id)
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="RESTORED",
                body={"systemId": participant.system_id},
            )

        if failures:
            # Backward recovery itself failed for some system: loud, on the DLQ, with
            # the saga parked as FAILED — never silently "compensated".
            deps.dead_letters.send(
                {"sagaId": manifest.saga_id, "operation": "compensate", "failures": failures}
            )
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="COMPENSATION_FAILED",
                body={"failures": failures, "trigger": trigger},
            )
            return {
                "status": STATUS_COMPENSATION_FAILED,
                "receipts": receipts,
                "compensated": compensated,
                "errors": failures,
            }

        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="SAGA_COMPENSATED",
            body={"trigger": trigger, "restored": compensated},
        )
        return {
            "status": STATUS_COMPENSATED,
            "receipts": receipts,
            "compensated": compensated,
        }

    return compensate
