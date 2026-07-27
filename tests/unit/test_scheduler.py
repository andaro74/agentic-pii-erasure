"""Scheduler protocol, EventBridge naming/idempotency, and the resume handler's two
defences (invariant 11): stale-wake filtering and delivery dedup.

The resume handler runs against a REAL langgraph graph paused at a real `interrupt()`
(with `InMemorySaver`), because "the filter reads the current gate's expectedWakes"
is a claim about the interaction with the checkpoint, not about a dict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import pii_erasure.scheduler.handler as resume_handler
from pii_erasure.saga.state import last_value
from pii_erasure.scheduler.base import ScheduleRequest, UnknownWakeReasonError
from pii_erasure.scheduler.eventbridge import EventBridgeScheduler, schedule_name

_AT = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


# ─── base protocol ────────────────────────────────────────────────────────────────────


def test_schedule_request_rejects_unknown_reasons_and_naive_times() -> None:
    with pytest.raises(UnknownWakeReasonError):
        ScheduleRequest(thread_id="t", at=_AT, reason="whenever")
    with pytest.raises(ValueError, match="timezone"):
        ScheduleRequest(thread_id="t", at=_AT.replace(tzinfo=None), reason="grace_elapsed")


def test_schedule_names_are_deterministic_bounded_and_distinct() -> None:
    one = schedule_name(stage="dev", thread_id="saga_" + "x" * 80, reason="sweep_t7")
    two = schedule_name(stage="dev", thread_id="saga_" + "x" * 80, reason="sweep_t7")
    other = schedule_name(stage="dev", thread_id="saga_" + "x" * 80, reason="sweep_t30")
    assert one == two, "re-execution after interrupt() must produce the same name"
    assert one != other
    assert len(one) <= 64
    import re

    assert re.fullmatch(r"[0-9a-zA-Z-_.]+", one), "EventBridge Scheduler name charset"


# ─── EventBridge implementation ───────────────────────────────────────────────────────


class _ConflictError(Exception):
    pass


class _FakeSchedulerClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

        class _Exceptions:
            ConflictException = _ConflictError

        self.exceptions = _Exceptions()

    def create_schedule(self, **kwargs: Any) -> None:
        if any(c["Name"] == kwargs["Name"] for c in self.created):
            raise _ConflictError()
        self.created.append(kwargs)


def _scheduler(client: _FakeSchedulerClient) -> EventBridgeScheduler:
    return EventBridgeScheduler(
        stage="dev",
        target_arn="arn:aws:lambda:us-test-1:0:function:resume",
        role_arn="arn:aws:iam::0:role/sched",
        dlq_arn="arn:aws:sqs:us-test-1:0:dlq",
        client=client,
    )


def test_recreating_the_same_schedule_is_a_noop_not_a_second_timer() -> None:
    client = _FakeSchedulerClient()
    scheduler = _scheduler(client)
    request = ScheduleRequest(thread_id="saga_1", at=_AT, reason="grace_elapsed")
    name_one = scheduler.schedule_resume(request)
    name_two = scheduler.schedule_resume(request)  # node re-executed after interrupt()
    assert name_one == name_two
    assert len(client.created) == 1


def test_schedule_carries_the_wake_payload_and_self_deletes() -> None:
    client = _FakeSchedulerClient()
    _scheduler(client).schedule_resume(
        ScheduleRequest(thread_id="saga_1", at=_AT, reason="sweep_t7")
    )
    created = client.created[0]
    assert created["ActionAfterCompletion"] == "DELETE"
    assert created["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert '"wake_reason": "sweep_t7"' in created["Target"]["Input"]
    assert created["Target"]["DeadLetterConfig"]["Arn"].endswith("dlq")
    assert created["ScheduleExpression"] == "at(2026-07-26T12:00:00)"


# ─── the resume handler ───────────────────────────────────────────────────────────────


class _GateState(TypedDict, total=False):
    status: Annotated[str, last_value]


class _FakeDedup:
    """In-memory IdempotencyLog with the same claim/complete/release surface."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any] | None] = {}

    def claim(self, *, system_id: str, key: str) -> dict[str, Any] | None:
        if key in self.records:
            prior = self.records[key]
            if prior is None:
                from pii_erasure.participants._base.idempotency import ReplayInFlightError

                raise ReplayInFlightError(key)
            return prior
        self.records[key] = None
        return None

    def complete(self, *, system_id: str, key: str, response: dict[str, Any]) -> None:
        self.records[key] = response

    def release(self, *, system_id: str, key: str) -> None:
        self.records.pop(key, None)


def _paused_graph(expected_wake: str, *, explode_on_resume: bool = False) -> Any:
    def gate(state: dict[str, Any]) -> dict[str, Any]:
        answer = interrupt({"gate": "test_gate", "expectedWakes": [expected_wake]})
        if explode_on_resume:
            raise RuntimeError("node failure after resume")
        return {"status": f"resumed_with_{answer['wake_reason']}"}

    builder = StateGraph(_GateState)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    graph.invoke({}, {"configurable": {"thread_id": "saga_r1"}}, durability="sync")
    return graph


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> _FakeDedup:
    dedup = _FakeDedup()
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "unused")
    monkeypatch.setattr(resume_handler, "IdempotencyLog", lambda _table: dedup)
    return dedup


def _wire(monkeypatch: pytest.MonkeyPatch, graph: Any) -> None:
    monkeypatch.setattr(resume_handler, "production_graph", lambda: graph)


def test_a_live_wake_resumes_exactly_once(rig: _FakeDedup, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _paused_graph("grace_elapsed"))
    event = {"thread_id": "saga_r1", "wake_reason": "grace_elapsed"}

    first = resume_handler.lambda_handler(event, None)
    assert first["status"] == "resumed_with_grace_elapsed"

    # At-least-once delivery replays the wake; the graph moved on, so the replay is
    # stale by inspection — caught by the filter before the dedup even matters.
    second = resume_handler.lambda_handler(event, None)
    assert second["status"] == "stale_wake"


def test_a_wake_the_current_gate_does_not_expect_is_dropped(
    rig: _FakeDedup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval_timeout that fires after approval: the saga now waits at a gate
    expecting grace_elapsed, and the stale timeout must not answer that question."""
    _wire(monkeypatch, _paused_graph("grace_elapsed"))
    result = resume_handler.lambda_handler(
        {"thread_id": "saga_r1", "wake_reason": "approval_timeout"}, None
    )
    assert result["status"] == "stale_wake"
    assert rig.records == {}, "a stale wake must not burn a delivery claim"


def test_a_duplicate_wake_hits_the_dedup_when_the_gate_still_matches(
    rig: _FakeDedup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, _paused_graph("grace_elapsed"))
    key = "saga_r1\x1fgrace_elapsed"
    rig.records[key] = {"status": "paused"}  # a prior delivery completed
    result = resume_handler.lambda_handler(
        {"thread_id": "saga_r1", "wake_reason": "grace_elapsed"}, None
    )
    assert result["status"] == "duplicate_wake"


def test_a_failed_resume_releases_the_claim_for_retry(
    rig: _FakeDedup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch, _paused_graph("grace_elapsed", explode_on_resume=True))
    with pytest.raises(RuntimeError, match="node failure"):
        resume_handler.lambda_handler(
            {"thread_id": "saga_r1", "wake_reason": "grace_elapsed"}, None
        )
    assert rig.records == {}, "the claim must be released so the retry is fresh"


def test_unknown_wake_reasons_fail_loudly(rig: _FakeDedup, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _paused_graph("grace_elapsed"))
    with pytest.raises(UnknownWakeReasonError):
        resume_handler.lambda_handler({"thread_id": "saga_r1", "wake_reason": "nope"}, None)
