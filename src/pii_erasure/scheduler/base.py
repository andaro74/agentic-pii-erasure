"""The scheduler protocol. Timers are ours now; this is the seam that keeps them small.

A saga node asks for "wake thread T at time A for reason R" and nothing more. The
EventBridge implementation lives in `eventbridge.py`; unit tests substitute an
in-memory recorder. No framework imports here — the protocol is plain Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

#: Every wake reason the platform ever schedules. Closed vocabulary on purpose: the
#: resume handler's idempotency key is ``(thread_id, wake_reason)``, and free-form
#: reasons would make "has this wake already been delivered?" unanswerable.
WAKE_REASONS: frozenset[str] = frozenset(
    {"approval_timeout", "grace_elapsed", "sweep_t7", "sweep_t30"}
)


class UnknownWakeReasonError(ValueError):
    """A wake reason outside the closed vocabulary."""


@dataclass(frozen=True)
class ScheduleRequest:
    thread_id: str
    at: datetime  # must be timezone-aware UTC
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in WAKE_REASONS:
            raise UnknownWakeReasonError(
                f"unknown wake reason {self.reason!r} — the vocabulary is {sorted(WAKE_REASONS)}"
            )
        if self.at.tzinfo is None:
            raise ValueError("schedule times must be timezone-aware UTC")


class Scheduler(Protocol):
    """Schedule a one-shot resume. Idempotent per (thread_id, reason).

    Idempotency matters at *creation* too: `interrupt()` re-executes its node from the
    top on resume, so the node that scheduled a wake before pausing will run the
    scheduling call again after waking. A second creation of the same (thread, reason)
    must be a no-op, not a second timer.
    """

    def schedule_resume(self, request: ScheduleRequest) -> str:
        """Create the schedule; return its name. Re-creation returns the same name."""
        ...
