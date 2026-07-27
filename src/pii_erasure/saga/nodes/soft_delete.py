"""Phase 2: soft-delete every participant, in order — identity revocation first (§5.2).

Backward recovery: any failure routes the saga to `compensate`, which restores what
was soft-deleted using the restore tokens collected here. Completed work is skipped on
replay via the receipts dict, and the participant-side idempotency log is the second
net under that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.contract import MutationResponse, Outcome, SoftDeleteRequest, Verb
from pii_erasure.contract.idempotency import idempotency_key
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.invoker import ParticipantCallError
from pii_erasure.saga.nodes._shared import digest_from_state, manifest_from_state, receipt_key
from pii_erasure.saga.ordering import execution_order

#: The status the routing edge reads. Not terminal — compensate follows.
STATUS_PHASE2_FAILED = "phase2_failed"


def make_soft_delete(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def soft_delete(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        digest = digest_from_state(state)
        existing = dict(state.get("receipts") or {})

        receipts: dict[str, Any] = {}
        restore_tokens: dict[str, Any] = {}
        residuals: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for participant in execution_order(manifest, phase=2):
            if "soft_delete" not in participant.planned_ops:
                continue
            key = receipt_key("soft_delete", participant.system_id)
            if key in existing:
                continue  # completed before a crash-replay — do not repeat the call

            request = SoftDeleteRequest(
                subject_ref=manifest.subject_ref,
                saga_id=manifest.saga_id,
                manifest_digest=digest,
                idempotency_key=idempotency_key(
                    saga_id=manifest.saga_id,
                    system_id=participant.system_id,
                    operation=Verb.SOFT_DELETE,
                    artifacts=participant.artifacts,
                ),
                artifacts=participant.artifacts,
            )
            try:
                body = deps.participants.call(
                    participant.system_id, "soft_delete", request.digested_body()
                )
                response = MutationResponse.model_validate(body)
            except (ParticipantCallError, ValueError) as error:
                errors.append(
                    {
                        "node": "soft_delete",
                        "systemId": participant.system_id,
                        "error": type(error).__name__,
                    }
                )
                deps.ledger.append(
                    saga_id=manifest.saga_id,
                    event_type="SOFT_DELETE_FAILED",
                    body={"systemId": participant.system_id, "error": type(error).__name__},
                )
                return {
                    "status": STATUS_PHASE2_FAILED,
                    "phase": 2,
                    "receipts": receipts,
                    "restore_tokens": restore_tokens,
                    "residuals": residuals,
                    "errors": errors,
                }

            if response.outcome is Outcome.REFUSED:
                # A refusal in phase 2 is a failure of the *plan*, and the safe answer
                # is to unwind — this is exactly what backward recovery is for.
                errors.append(
                    {
                        "node": "soft_delete",
                        "systemId": participant.system_id,
                        "error": "REFUSED",
                    }
                )
                deps.ledger.append(
                    saga_id=manifest.saga_id,
                    event_type="SOFT_DELETE_REFUSED",
                    body={"systemId": participant.system_id},
                )
                return {
                    "status": STATUS_PHASE2_FAILED,
                    "phase": 2,
                    "receipts": receipts,
                    "restore_tokens": restore_tokens,
                    "residuals": residuals,
                    "errors": errors,
                }

            receipts[key] = body
            if response.restore_token:
                restore_tokens[participant.system_id] = response.restore_token
            residuals.extend(r.digested_body() for r in response.residual)
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="SOFT_DELETE_APPLIED",
                body={
                    "systemId": participant.system_id,
                    "outcome": response.outcome.value,
                    "affected": response.affected,
                },
            )

        return {
            "phase": 2,
            "receipts": receipts,
            "restore_tokens": restore_tokens,
            "residuals": residuals,
        }

    return soft_delete
