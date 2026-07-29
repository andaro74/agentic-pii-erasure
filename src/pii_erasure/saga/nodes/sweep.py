"""Re-verification sweeps at T+7 and T+30 — resurrection detection (§5.3).

Artifacts reappearing after a verified erasure indicate a *systemic* write path that
bypasses the tombstone check — a distinct alarm from a deletion failure, raised on the
DLQ with its own event type.

Mechanics under re-execution (langgraph re-runs an interrupted node from the top):
the two pauses are consecutive `interrupt()` calls, matched to resume values by order.
On each resume the node re-executes; already-answered interrupts return their stored
values instantly, already-created schedules resolve by deterministic name. One
tolerated artefact: a re-execution recomputes `now()` and may recreate an
already-fired schedule for a wake that was already delivered — the resume handler's
stale-wake filter and delivery dedup exist precisely to absorb that (invariant 11).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from langgraph.types import interrupt

from pii_erasure.approval.gate import GATE_SWEEP, ResumeValidationError
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.nodes._shared import manifest_from_state, verify_all_participants
from pii_erasure.saga.state import STATUS_COMPLETED
from pii_erasure.scheduler.base import ScheduleRequest

STATUS_RESURRECTION = "resurrection_incident"

_SWEEPS = ("sweep_t7", "sweep_t30")


def make_sweep(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def sweep(state: dict[str, Any]) -> dict[str, Any]:
        manifest = manifest_from_state(state)
        receipts: dict[str, Any] = {}
        sweeps_done: list[str] = []

        for reason, delay in zip(_SWEEPS, deps.sweep_delays_seconds, strict=True):
            deps.scheduler.schedule_resume(
                ScheduleRequest(
                    thread_id=manifest.saga_id,
                    at=deps.now() + timedelta(seconds=delay),
                    reason=reason,
                )
            )
            answer = interrupt(
                {
                    "gate": GATE_SWEEP,
                    "expectedWakes": [reason],
                    "sagaId": manifest.saga_id,
                }
            )
            wake = answer.get("wake_reason") if isinstance(answer, dict) else None
            if wake != reason:
                raise ResumeValidationError(f"sweep resumed with {wake!r}, expected {reason!r}")

            swept, unexpected = verify_all_participants(
                deps,
                manifest,
                verify_prefix=reason,
                # A residual disclosed at hard-delete time is still lawfully there at
                # T+7 and T+30. What makes a sweep interesting is artifacts NOBODY
                # disclosed — including *more* rows under a held locator than the hold
                # retained, which the count comparison catches.
                receipts_so_far=state.get("receipts"),
            )
            receipts.update(swept)
            sweeps_done.append(reason)

            if unexpected:
                deps.dead_letters.send(
                    {
                        "sagaId": manifest.saga_id,
                        "operation": reason,
                        "resurrection": unexpected,
                    }
                )
                deps.ledger.append(
                    saga_id=manifest.saga_id,
                    event_type="RESURRECTION_INCIDENT",
                    body={"sweep": reason, "unexpected": unexpected},
                )
                return {
                    "status": STATUS_RESURRECTION,
                    "receipts": receipts,
                    "sweeps_done": sweeps_done,
                    "errors": [{"node": "sweep", "error": "resurrection", "sweep": reason}],
                }

            deps.ledger.append(
                saga_id=manifest.saga_id, event_type="SWEEP_CLEAN", body={"sweep": reason}
            )

        deps.ledger.append(saga_id=manifest.saga_id, event_type="SAGA_COMPLETED", body={})
        return {
            "status": STATUS_COMPLETED,
            "receipts": receipts,
            "sweeps_done": sweeps_done,
        }

    return sweep
