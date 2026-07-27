"""Phase 3: forward recovery only (invariant 6).

**One participant per node execution.** The node processes the first pending
participant in phase-3 order, returns its receipt, and the router loops back until
none remain — so with ``durability="sync"`` every completed hard delete is
checkpointed *individually*. Kill the executor between any two participants and the
re-invocation resumes from the checkpoint with the completed ones skipped by their
receipts: zero duplicate participant calls, with the participant idempotency log as
the second net under that.

If a participant's bounded retries exhaust (or it REFUSES — deterministic, retries
cannot change it), the failure goes to the SQS DLQ and the node `interrupt()`s at the
``stuck`` gate: the Lambda returns, nothing rolls back, and the saga waits for an
operator to remediate and resume with ``{"wake_reason": "retry_phase3"}`` — the
diagram's "Stuck → P3: manual remediation" arc (§5). There is no compensation here,
no rollback, and no path to `restore` — a compensating transaction that recreates the
subject's data converts a failed erasure into an active breach. This module must
never import or reference the restore verb or the compensate module; a test asserts
that on the AST.

Before anything irreversible runs, two bindings are re-checked in-process on every
entry: the approval token verifies against the manifest digest, and the stored
manifest re-digests to the digest that was approved. A mismatch is the
post-approval-mutation attack (§8.3) and aborts the saga — abort, not compensate:
nothing irreversible has happened yet, and the parked soft-deleted state is exactly
where a security investigation wants the data left.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.types import interrupt

from pii_erasure.approval.gate import GATE_STUCK, ResumeValidationError
from pii_erasure.approval.tokens import ApprovalTokenError
from pii_erasure.contract import HardDeleteRequest, MutationResponse, Outcome, Verb
from pii_erasure.contract.idempotency import idempotency_key
from pii_erasure.manifest import Manifest, assert_digest
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.invoker import ParticipantCallError
from pii_erasure.saga.nodes._shared import digest_from_state, receipt_key
from pii_erasure.saga.ordering import execution_order
from pii_erasure.saga.state import STATUS_ABORTED, STATUS_RUNNING


def pending_hard_deletes(state: dict[str, Any]) -> list[str]:
    """Phase-3 participants without a receipt yet, in execution order. Used by the
    node (to pick its next unit of work) and by the router (to decide the loop)."""
    manifest = Manifest.model_validate(state["manifest"])
    receipts = state.get("receipts") or {}
    return [
        p.system_id
        for p in execution_order(manifest, phase=3)
        if receipt_key("hard_delete", p.system_id) not in receipts
    ]


def make_hard_delete(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def hard_delete(state: dict[str, Any]) -> dict[str, Any]:
        manifest = Manifest.model_validate(state["manifest"])
        digest = digest_from_state(state)
        approval = state.get("approval") or {}
        token = approval.get("token")

        try:
            if not token:
                raise ApprovalTokenError("phase 3 reached without an approval token")
            deps.tokens.verify(token, expected_digest=digest, saga_id=manifest.saga_id)
            # Re-digest the stored body: a checkpoint edited after approval must fail
            # here, before any participant is asked to do anything irreversible.
            if assert_digest(manifest) != digest:
                raise ApprovalTokenError("stored manifest digest diverged from approval")
        except (ApprovalTokenError, ValueError) as error:
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="PHASE3_ABORTED",
                body={"reason": str(error)},
            )
            return {
                "status": STATUS_ABORTED,
                "errors": [{"node": "hard_delete", "error": type(error).__name__}],
            }

        pending = pending_hard_deletes(state)
        if not pending:
            deps.tombstones.record(manifest.subject_ref, saga_id=manifest.saga_id)
            deps.ledger.append(saga_id=manifest.saga_id, event_type="TOMBSTONED", body={})
            return {"status": STATUS_RUNNING, "tombstoned": True}

        participant = next(p for p in manifest.participants if p.system_id == pending[0])
        request = HardDeleteRequest(
            subject_ref=manifest.subject_ref,
            saga_id=manifest.saga_id,
            manifest_digest=digest,
            idempotency_key=idempotency_key(
                saga_id=manifest.saga_id,
                system_id=participant.system_id,
                operation=Verb.HARD_DELETE,
                artifacts=participant.artifacts,
            ),
            artifacts=participant.artifacts,
            approval_token=str(token),
        )

        response: MutationResponse | None = None
        last_error = "unknown"
        for _attempt in range(deps.hard_delete_attempts):
            try:
                body = deps.participants.call(
                    participant.system_id, "hard_delete", request.digested_body()
                )
                response = MutationResponse.model_validate(body)
                break
            except (ParticipantCallError, ValueError) as error:
                last_error = type(error).__name__
                continue

        if response is None or response.outcome is Outcome.REFUSED:
            # Retries exhausted, or a deterministic refusal that retries cannot
            # change. Forward recovery has run out of road in this process: DLQ,
            # then PAUSE — a human remediates and resumes. Never a rollback.
            reason = "REFUSED" if response is not None else last_error
            deps.dead_letters.send(
                {
                    "sagaId": manifest.saga_id,
                    "systemId": participant.system_id,
                    "phase": 3,
                    "operation": "hard_delete",
                    "reason": reason,
                }
            )
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="PHASE3_STUCK",
                body={"systemId": participant.system_id, "reason": reason},
            )
            answer = interrupt(
                {
                    "gate": GATE_STUCK,
                    # No *scheduled* wake may ever resume this gate — remediation is
                    # a human act, delivered through the executor's resume action.
                    "expectedWakes": [],
                    "sagaId": manifest.saga_id,
                    "systemId": participant.system_id,
                    "reason": reason,
                }
            )
            wake = answer.get("wake_reason") if isinstance(answer, dict) else None
            if wake != "retry_phase3":
                raise ResumeValidationError(
                    f"stuck gate resumed with {wake!r} — expected 'retry_phase3'"
                )
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="PHASE3_RETRY",
                body={"systemId": participant.system_id},
            )
            # Return nothing: the router sees the participant still pending and
            # re-enters this node, which retries it against the remediated world.
            return {}

        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="HARD_DELETE_APPLIED",
            body={
                "systemId": participant.system_id,
                "outcome": response.outcome.value,
                "affected": response.affected,
                "residualCount": len(response.residual),
            },
        )
        return {
            "status": STATUS_RUNNING,
            "receipts": {
                receipt_key("hard_delete", participant.system_id): response.digested_body()
            },
            "residuals": [r.digested_body() for r in response.residual],
        }

    return hard_delete
