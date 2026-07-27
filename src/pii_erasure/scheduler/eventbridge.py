"""EventBridge Scheduler one-shot schedules → the resume Lambda.

Schedule names are deterministic per ``(thread_id, reason)`` — a re-executed node
re-creating its schedule hits `ConflictException`, which is absorbed as the idempotent
success it is. `ActionAfterCompletion=DELETE` makes fired schedules self-cleaning, so
an idle stack holds no schedule inventory (the no-continuous-billing rule; schedules
are free until they fire anyway).

API shape verified against the installed botocore service model (2021-06-30):
`CreateSchedule` requires Name, ScheduleExpression, FlexibleTimeWindow, and
Target{Arn, RoleArn}; names are ≤64 chars of ``[0-9a-zA-Z-_.]``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Any

import boto3

from pii_erasure.scheduler.base import ScheduleRequest


def schedule_name(*, stage: str, thread_id: str, reason: str) -> str:
    """Deterministic, collision-resistant, ≤64 chars.

    The hash carries the identity; the prefix and reason are for the human reading the
    console. `thread_id` itself may exceed the budget, so it is never embedded raw.
    """
    digest = hashlib.sha256(f"{thread_id}\x00{reason}".encode()).hexdigest()[:24]
    return f"asdp-{stage}-{reason}-{digest}"[:64]


class EventBridgeScheduler:
    """The production `Scheduler` (see base.py for the protocol contract)."""

    def __init__(
        self,
        *,
        stage: str,
        target_arn: str,
        role_arn: str,
        dlq_arn: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._stage = stage
        self._target_arn = target_arn
        self._role_arn = role_arn
        self._dlq_arn = dlq_arn
        self._scheduler = client or boto3.client("scheduler")

    def schedule_resume(self, request: ScheduleRequest) -> str:
        name = schedule_name(stage=self._stage, thread_id=request.thread_id, reason=request.reason)
        target: dict[str, Any] = {
            "Arn": self._target_arn,
            "RoleArn": self._role_arn,
            "Input": json.dumps({"thread_id": request.thread_id, "wake_reason": request.reason}),
            # At-least-once is the contract we design for (invariant 11), but there is
            # no reason to make duplicates *likely*: bounded retries, then the DLQ.
            "RetryPolicy": {"MaximumRetryAttempts": 3},
        }
        if self._dlq_arn:
            target["DeadLetterConfig"] = {"Arn": self._dlq_arn}

        expression = f"at({request.at.strftime('%Y-%m-%dT%H:%M:%S')})"
        # Conflict suppressed by design: the node re-executed after its interrupt,
        # and the timer already exists under this deterministic name.
        with contextlib.suppress(self._scheduler.exceptions.ConflictException):
            self._scheduler.create_schedule(
                Name=name,
                ScheduleExpression=expression,
                ScheduleExpressionTimezone="UTC",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target=target,
                ActionAfterCompletion="DELETE",
                Description=f"ASDP resume: {request.reason} for a paused saga",
            )
        return name
