"""The approval gate: `interrupt()` — the Lambda RETURNS here, for days (§8.2).

The pause is data, not a process: `interrupt()` checkpoints the graph and the
invocation ends. Days later `Command(resume=…)` arrives — from the approval API (M8),
the integration harness, or the timeout schedule — carrying a decision and, for an
approval, the digest the human reviewed. Digest mismatch is the TOCTOU signal (§8.3):
the saga unwinds to re-approval rather than executing a plan nobody saw.

On resume the node RE-EXECUTES from the top (verified against langgraph 1.2.9, not
assumed) — so everything before `interrupt()` must be idempotent. The timeout
schedule's deterministic name makes its re-creation a no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from langgraph.types import interrupt

from pii_erasure.approval.gate import ResumeValidationError, interrupt_payload, parse_resume
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import digest_from_state, iso, manifest_from_state
from pii_erasure.saga.state import STATUS_RUNNING
from pii_erasure.scheduler.base import ScheduleRequest

STATUS_APPROVAL_DENIED = "approval_denied"
STATUS_APPROVAL_INVALID = "approval_invalid"


def make_approval_gate(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def approval_gate(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        digest = digest_from_state(state)

        deps.scheduler.schedule_resume(
            ScheduleRequest(
                thread_id=manifest.saga_id,
                at=deps.now() + timedelta(seconds=deps.approval_timeout_seconds),
                reason="approval_timeout",
            )
        )

        answer = interrupt(interrupt_payload(manifest))

        try:
            resume = parse_resume(answer, expected_digest=digest)
        except ResumeValidationError as error:
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="APPROVAL_INVALID",
                body={"reason": str(error)},
            )
            return {
                "status": STATUS_APPROVAL_INVALID,
                "errors": [{"node": "approval_gate", "error": type(error).__name__}],
            }

        if resume.decision != "approve":
            # Denial and timeout land in the same place: unwind. Silence implies DENY.
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="APPROVAL_DENIED",
                body={"decision": resume.decision, "approver": resume.approver},
            )
            return {"status": STATUS_APPROVAL_DENIED}

        approved_at = iso(deps.now())
        token = deps.tokens.mint(
            manifest_digest=digest,
            saga_id=manifest.saga_id,
            approver=resume.approver,
            approved_at=approved_at,
        )
        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="APPROVAL_GRANTED",
            body={"approver": resume.approver, "manifestDigest": digest, "approvedAt": approved_at},
        )
        return {
            "status": STATUS_RUNNING,
            "approval": {
                "decision": "approve",
                "approver": resume.approver,
                "digest": digest,
                "token": token,
            },
        }

    return approval_gate
