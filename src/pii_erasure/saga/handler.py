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

**A resume is validated against the current gate before it is delivered** (V9-3).
LangGraph persists a `Command(resume=…)` value against the pending interrupt *before*
the node consumes it, so a wrong-shaped resume does not merely fail once: the bad
value is stored, and every subsequent legitimate resume replays it and fails
identically. A duplicate approval arriving after the saga has moved on to the grace
or sweep gate would wedge a live erasure request past a statutory deadline — no
credentials required beyond the ability to call this function once. So a mismatched
resume is REFUSED without touching the graph, which is the same defence
`scheduler/handler.py` applies to wake reasons, at the other entry point.

`durability="sync"` everywhere: the checkpoint is written before the next step runs.
It is the system of record, and "the state that would have been written" is not state.

The Lambda RETURNS while the saga is paused — the pause is a checkpoint row, not a
held invocation. `thread_id` == ``sagaId`` == the trace correlation key.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from pii_erasure.approval.gate import GATE_APPROVAL, GATE_GRACE, GATE_STUCK, GATE_SWEEP
from pii_erasure.observability.logging import configure_logging, get_logger
from pii_erasure.saga.graph import production_graph

configure_logging()
_log = get_logger(__name__)

_START_FIELDS = ("saga_id", "subject_ref", "request_id", "tenant_id")

#: The key each gate's resume value must carry. A gate absent from this map accepts
#: nothing — an unknown gate is a new pause someone added without deciding what may
#: legitimately resume it, and guessing on its behalf is how the wedge gets back in.
_GATE_RESUME_KEY: dict[str, str] = {
    GATE_APPROVAL: "decision",
    GATE_GRACE: "wake_reason",
    GATE_SWEEP: "wake_reason",
    GATE_STUCK: "wake_reason",
}


class SagaRequestError(ValueError):
    """The invocation event is not a valid start or resume."""


def _answers_gate(gate: str, resume_value: Any) -> bool:
    """Is this resume value an answer to the question the saga is actually asking?

    Shape only — whether the *content* is acceptable (a digest that matches, a wake
    the gate expects) stays with the node that asked, which is where the domain rules
    and their ledger entries live. This check exists solely to keep a payload meant
    for a different gate from ever being persisted against this one.
    """
    required = _GATE_RESUME_KEY.get(gate)
    if required is None:
        return False
    return isinstance(resume_value, dict) and required in resume_value


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
        interrupts = graph.get_state(config).interrupts
        if not interrupts:
            _log.warning("resume_refused_not_paused", thread_id=thread_id)
            return {"thread_id": thread_id, "status": "not_paused"}

        resume_value = event.get("resume")
        gate = str((interrupts[0].value or {}).get("gate", ""))
        if not _answers_gate(gate, resume_value):
            # Refused, not raised, and above all NOT delivered: reaching the graph is
            # what persists the value and wedges the thread (V9-3).
            _log.warning("resume_rejected", thread_id=thread_id, gate=gate)
            return {"thread_id": thread_id, "status": "resume_rejected", "gate": gate}

        result = graph.invoke(Command(resume=resume_value), config, durability="sync")
        return _summary(thread_id, result)

    if action == "describe":
        return _describe(graph, str(event["thread_id"]))

    if action == "threads":
        return _threads(graph, limit=int(event.get("limit", 50)))

    raise SagaRequestError(f"unknown action {action!r}")


def _describe(graph: Any, thread_id: str) -> dict[str, Any]:
    """One thread, read-only — what it is waiting for and the plan it is waiting on.

    Read-only is the load-bearing word. The approval API (M8) needs the manifest and the
    pending gate to render a review, and it must obtain them **without** delivering a
    resume: LangGraph persists a resume value against the pending interrupt before the
    node consumes it, so a "read" implemented as a no-op resume would wedge the thread it
    was inspecting (V9-3, the same trap the resume path guards).

    It also carries the digest the approver must echo back. The API compares them before
    minting anything, which is invariant 3 enforced one layer earlier than the node — not
    instead of it. `nodes/approval_gate.py` still checks, because a caller that skips the
    API must still fail.
    """
    state = graph.get_state(_config(thread_id))
    if not state.values:
        return {"thread_id": thread_id, "status": "unknown"}
    interrupts = state.interrupts
    payload = (interrupts[0].value or {}) if interrupts else {}
    return {
        "thread_id": thread_id,
        "status": "paused" if interrupts else str(state.values.get("status", "running")),
        "gate": payload.get("gate"),
        "interrupt": payload,
        "subject_ref": state.values.get("subject_ref"),
        "tenant_id": state.values.get("tenant_id"),
        "manifest": state.values.get("manifest"),
        "manifest_digest": state.values.get("manifest_digest"),
    }


def _threads(graph: Any, *, limit: int) -> dict[str, Any]:
    """Every thread the checkpointer knows, newest checkpoint per thread.

    The checkpointer is the only source consulted — deliberately. A separate index of
    live sagas would be a second place saga state lives, and reconciling the two is
    exactly the divergence ADR-016 removed. The cost is that this walks checkpoints
    rather than querying an index, so it is an operator command and not a hot path;
    `limit` bounds it and the response says whether it truncated.
    """
    seen: dict[str, dict[str, Any]] = {}
    truncated = False
    for tuple_ in graph.checkpointer.list(None, limit=limit * 20):
        thread_id = str(tuple_.config.get("configurable", {}).get("thread_id", ""))
        if not thread_id or thread_id in seen:
            continue
        if len(seen) >= limit:
            truncated = True
            break
        seen[thread_id] = _describe(graph, thread_id)
    return {
        "threads": sorted(seen.values(), key=lambda t: str(t["thread_id"])),
        "truncated": truncated,
    }


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
