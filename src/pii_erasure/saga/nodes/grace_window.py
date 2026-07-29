"""The statutory grace window: schedule the wake, then pause.

The subject may revoke the erasure request during this window — that resume routes to
compensation, restoring the soft-deleted state. A zero-length window (fixture
manifests, dev overrides) skips the pause entirely rather than scheduling a wake in
the past, which EventBridge Scheduler would reject.

`GRACE_SECONDS_OVERRIDE` **caps** the window; it does not set it. See below.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from langgraph.types import interrupt

from pii_erasure.approval.gate import GATE_GRACE, ResumeValidationError
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import digest_from_state, manifest_from_state
from pii_erasure.scheduler.base import ScheduleRequest

STATUS_REVOKED = "revoked"

#: Resume values this gate accepts. "revoked" arrives from the subject via the API,
#: not from a schedule — so it is a valid *resume* but never an expected *wake*.
_VALID_WAKES = frozenset({"grace_elapsed", "revoked"})


def make_grace_window(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def grace_window(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        # The override is a CEILING, not a replacement. Its job (V11-1) is to stop a dev
        # saga sitting here for the manifest's 30 days. A manifest that already asks for
        # less is already shorter than dev needs, and *lengthening* it was never the
        # intent this file documents — the integration suite's fixture manifests ask for
        # zero, and a replacement silently gave them 120 seconds and a pause at a gate
        # their assertions say is skipped (V12-1).
        manifest_seconds = manifest.grace_window_days * 86400
        override = deps.grace_seconds_override
        seconds = min(manifest_seconds, override) if override is not None else manifest_seconds
        if seconds <= 0:
            deps.ledger.append(
                saga_id=manifest.saga_id,
                event_type="GRACE_WINDOW_SKIPPED",
                body={"graceWindowDays": manifest.grace_window_days},
            )
            return {}

        deps.scheduler.schedule_resume(
            ScheduleRequest(
                thread_id=manifest.saga_id,
                at=deps.now() + timedelta(seconds=seconds),
                reason="grace_elapsed",
            )
        )
        answer = interrupt(
            {
                "gate": GATE_GRACE,
                "expectedWakes": ["grace_elapsed"],
                "sagaId": manifest.saga_id,
                "manifestDigest": digest_from_state(state),
                "graceSeconds": seconds,
            }
        )

        wake = answer.get("wake_reason") if isinstance(answer, dict) else None
        if wake not in _VALID_WAKES:
            raise ResumeValidationError(
                f"grace window resumed with {wake!r} — expected one of {sorted(_VALID_WAKES)}"
            )
        if wake == "revoked":
            deps.ledger.append(saga_id=manifest.saga_id, event_type="REQUEST_REVOKED", body={})
            return {"status": STATUS_REVOKED}

        deps.ledger.append(saga_id=manifest.saga_id, event_type="GRACE_ELAPSED", body={})
        return {}

    return grace_window
