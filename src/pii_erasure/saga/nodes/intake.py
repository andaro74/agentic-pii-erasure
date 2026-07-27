"""Intake: validate identity fields, consult the tombstone registry, open the ledger.

The subject arrives as a pseudonymous handle — identity resolution (raw PII → handle)
happens *outside* the reasoning and execution planes, so nothing after this node ever
sees a name or an email (invariant 5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.state import STATUS_ALREADY_TOMBSTONED, STATUS_RUNNING

_REQUIRED = ("saga_id", "subject_ref", "request_id", "tenant_id")


class IntakeError(ValueError):
    """The start input is incomplete. Refused before anything is recorded."""


def make_intake(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def intake(state: dict[str, Any]) -> dict[str, Any]:
        missing = [field for field in _REQUIRED if not state.get(field)]
        if missing:
            raise IntakeError(f"start input is missing {missing}")

        subject_ref = str(state["subject_ref"])
        saga_id = str(state["saga_id"])

        if deps.tombstones.is_tombstoned(subject_ref):
            # An erased subject cannot be re-erased — and more importantly, nothing
            # downstream may run: a tombstoned handle showing up here usually means a
            # duplicate request, and the correct answer is the certificate we already
            # issued, not a second saga.
            deps.ledger.append(
                saga_id=saga_id,
                event_type="SAGA_REFUSED_TOMBSTONED",
                body={"subjectRef": subject_ref},
            )
            return {"status": STATUS_ALREADY_TOMBSTONED}

        deps.ledger.append(
            saga_id=saga_id,
            event_type="SAGA_STARTED",
            body={
                "subjectRef": subject_ref,
                "requestId": str(state["request_id"]),
                "tenantId": str(state["tenant_id"]),
            },
        )
        return {"phase": 1, "status": STATUS_RUNNING}

    return intake
