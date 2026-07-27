"""The saga-executor Lambda entrypoint — drives the graph to the next interrupt or END.

Two actions:

* ``start`` — begin a saga for one subject with a provided manifest (M5's hand-written
  fixture; the Runtime-synthesised manifest arrives at M7). A thread that already has
  a checkpoint refuses to start again: re-running a live saga from the top is never
  what a duplicate request means.
* ``resume`` — deliver `Command(resume=…)` to a paused thread. The approval decision
  arrives this way (from the HTTP API at M8, from the integration harness until
  then); scheduler wakes arrive via `scheduler/handler.py`, which adds stale-wake
  filtering and delivery dedup on top of this same graph.

`durability="sync"` everywhere: the checkpoint is written before the next step runs.
It is the system of record, and "the state that would have been written" is not state.

The Lambda RETURNS while the saga is paused — the pause is a checkpoint row, not a
held invocation. `thread_id` == ``sagaId`` == the trace correlation key.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from pii_erasure.observability.logging import configure_logging, get_logger
from pii_erasure.saga.graph import production_graph

configure_logging()
_log = get_logger(__name__)

_START_FIELDS = ("saga_id", "subject_ref", "request_id", "tenant_id")


class SagaRequestError(ValueError):
    """The invocation event is not a valid start or resume."""


def _config(thread_id: str) -> dict[str, Any]:
    # Phase 3 loops one participant per superstep (see nodes/hard_delete.py), so a
    # full arc uses ~25+ supersteps — above langgraph's default recursion limit.
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    graph = production_graph()
    action = event.get("action", "start")

    if action == "start":
        saga = event.get("saga") or {}
        missing = [field for field in _START_FIELDS if not saga.get(field)]
        if missing:
            raise SagaRequestError(f"start event is missing saga.{missing}")
        thread_id = str(saga["saga_id"])
        config: dict[str, Any] = _config(thread_id)

        if graph.get_state(config).values:
            _log.warning("start_refused_existing_thread", thread_id=thread_id)
            return {"thread_id": thread_id, "status": "already_started"}

        input_state = {
            "saga_id": thread_id,
            "subject_ref": str(saga["subject_ref"]),
            "request_id": str(saga["request_id"]),
            "tenant_id": str(saga["tenant_id"]),
            "provided_manifest": saga.get("manifest"),
        }
        result = graph.invoke(input_state, config, durability="sync")
        return _summary(thread_id, result)

    if action == "resume":
        thread_id = str(event["thread_id"])
        config = _config(thread_id)
        if not graph.get_state(config).interrupts:
            _log.warning("resume_refused_not_paused", thread_id=thread_id)
            return {"thread_id": thread_id, "status": "not_paused"}
        result = graph.invoke(Command(resume=event.get("resume")), config, durability="sync")
        return _summary(thread_id, result)

    raise SagaRequestError(f"unknown action {action!r}")


def _summary(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """What the caller learns. Pseudonymous throughout — the full state stays in the
    checkpointer, and this summary is what lands in CloudWatch."""
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        payload = interrupts[0].value or {}
        summary = {
            "thread_id": thread_id,
            "status": "paused",
            "gate": payload.get("gate"),
            "interrupt": payload,
        }
    else:
        summary = {
            "thread_id": thread_id,
            "status": str(result.get("status", "running")),
            "manifest_digest": result.get("manifest_digest"),
            "residual_count": len(result.get("residuals") or []),
            "errors": result.get("errors") or [],
        }
    _log.info("saga_step", thread_id=thread_id, status=summary["status"])
    return summary
