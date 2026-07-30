"""The resume Lambda: load the checkpoint, deliver `Command(resume=…)`.

A paused saga is a checkpoint row and nothing else — no compute anywhere. This handler
is the *only* thing a timer can reach, and it is defensive in two independent ways
(invariant 11):

1. **Stale-wake filtering.** Every interrupt payload names the wakes it expects
   (`expectedWakes`). An `approval_timeout` that fires after a human already approved
   would otherwise resume the *grace-window* interrupt with an answer to a question it
   never asked. The filter reads the current interrupt from the checkpoint and drops
   any wake the current gate does not expect. Filtering runs BEFORE the idempotency
   claim on purpose — a stale wake must not burn the claim of a live one, and a crash
   after a successful resume heals here (the graph has moved on, so the replay is
   stale by inspection).
2. **Delivery dedup.** A conditional claim on ``(thread_id, wake_reason)`` in the
   idempotency table, under its own partition. EventBridge Scheduler is at-least-once,
   and a duplicate resume of a phase-3 node is a duplicate deletion attempt —
   participant idempotency keys would catch it downstream, but the handler must not
   rely on that alone.

Failure honesty: if the graph run raises, the claim is released so the wake can be
retried; if a *concurrent* invocation holds the claim, this one reports and stops
rather than racing it. A claim orphaned by a hard crash (timeout, OOM) mid-run is
surfaced as `concurrent_wake_in_flight` and is the operator `threads`/`resume`
tooling's case (M8) — never silently absorbed.
"""

from __future__ import annotations

import os
from typing import Any

from langgraph.types import Command

from pii_erasure.observability.logging import configure_logging, get_logger
from pii_erasure.observability.metrics import Dimensions, emit
from pii_erasure.participants._base.idempotency import IdempotencyLog, ReplayInFlightError
from pii_erasure.saga.graph import production_graph
from pii_erasure.scheduler.base import WAKE_REASONS, UnknownWakeReasonError

RESUME_SYSTEM_ID = "saga-resume"


def _dimensions() -> Dimensions:
    """Stage from the environment, plane fixed. No thread id: EMF bills one custom
    metric per unique dimension combination, and a per-saga dimension would bill per
    erasure request AND write a correlation key into a metrics surface."""
    return Dimensions(stage=os.environ.get("PII_ERASURE_STAGE", "dev"), plane="resume")


configure_logging()
_log = get_logger(__name__)


def _resume_value(wake_reason: str) -> dict[str, Any]:
    """Translate a timer firing into the answer the paused gate understands."""
    if wake_reason == "approval_timeout":
        # Silence implies DENY (§8.2). The gate treats timeout as a denial.
        return {"decision": "timeout", "approver": "scheduler"}
    return {"wake_reason": wake_reason}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    thread_id = str(event["thread_id"])
    wake_reason = str(event["wake_reason"])
    if wake_reason not in WAKE_REASONS:
        raise UnknownWakeReasonError(f"unknown wake reason {wake_reason!r}")

    log = _log.bind(thread_id=thread_id, wake_reason=wake_reason)
    graph = production_graph()
    # recursion_limit matches saga/handler.py: phase 3 loops one participant per
    # superstep, and a resumed saga may run the whole remainder of the arc.
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    snapshot = graph.get_state(config)
    if not snapshot.interrupts:
        log.info("stale_wake", detail="saga is not paused")
        return {"status": "stale_wake", "thread_id": thread_id, "wake_reason": wake_reason}
    payload = snapshot.interrupts[0].value or {}
    expected = payload.get("expectedWakes", [])
    if wake_reason not in expected:
        log.info(
            "stale_wake", detail="current gate does not expect this wake", gate=payload.get("gate")
        )
        return {"status": "stale_wake", "thread_id": thread_id, "wake_reason": wake_reason}

    dedup = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    key = f"{thread_id}\x1f{wake_reason}"
    try:
        prior = dedup.claim(system_id=RESUME_SYSTEM_ID, key=key)
    except ReplayInFlightError:
        log.warning("concurrent_wake_in_flight")
        return {
            "status": "concurrent_wake_in_flight",
            "thread_id": thread_id,
            "wake_reason": wake_reason,
        }
    if prior is not None:
        log.info("duplicate_wake")
        # The delivery dedup firing is not an error — Scheduler is at-least-once by
        # design. It is a metric because a RISE in it means the stale-wake filter above
        # stopped catching redeliveries, and that is invariant 11 leaking (§10.1).
        emit("scheduler.duplicate_wake", 1, _dimensions())
        return {"status": "duplicate_wake", "thread_id": thread_id, "wake_reason": wake_reason}

    try:
        result = graph.invoke(Command(resume=_resume_value(wake_reason)), config, durability="sync")
    except Exception:
        dedup.release(system_id=RESUME_SYSTEM_ID, key=key)
        # Emitted before the re-raise, because the re-raise is what makes Lambda record a
        # failure and this is the only place that knows a RESUME failed rather than a
        # participant. That distinction is the whole point of the metric: a resume failure
        # is an upgrade defect (ADR-016), and it is the shape a silently stranded erasure
        # request takes on its way to a missed statutory deadline.
        emit("checkpoint.resume_failure", 1, _dimensions(), wake_reason=wake_reason)
        raise

    paused = "__interrupt__" in result
    summary = {
        "status": "paused" if paused else str(result.get("status", "running")),
        "thread_id": thread_id,
        "wake_reason": wake_reason,
    }
    dedup.complete(system_id=RESUME_SYSTEM_ID, key=key, response=summary)
    log.info("resumed", **summary)
    return summary
