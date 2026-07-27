"""Saga dependencies — the seam between deterministic nodes and the AWS clients.

Nodes are plain functions over state (invariant 2); everything with a network behind
it arrives through this dataclass, so unit tests substitute in-memory fakes and the
deployed gate exercises the real services. Nothing here constructs a model client —
there is no field a model client could even live in, which is the point.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import boto3

from pii_erasure.approval.tokens import TokenMinter
from pii_erasure.ledger.writer import LedgerWriter
from pii_erasure.manifest import ManifestSigner
from pii_erasure.saga.invoker import LambdaParticipantInvoker
from pii_erasure.saga.tombstone import TombstoneRegistry
from pii_erasure.scheduler.base import Scheduler
from pii_erasure.scheduler.eventbridge import EventBridgeScheduler

#: Default approval window: 14 days, then the timeout wake fires and silence implies
#: DENY (§8.2). Overridable per stage — integration shortens it, never removes it.
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 14 * 24 * 60 * 60

#: Post-erasure verification sweeps (§5.3): T+7 and T+30.
DEFAULT_SWEEP_DELAYS_SECONDS = (7 * 24 * 60 * 60, 30 * 24 * 60 * 60)


class ParticipantInvoker(Protocol):
    def call(self, system_id: str, tool: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class Ledger(Protocol):
    def append(self, *, saga_id: str, event_type: str, body: dict[str, Any]) -> Any: ...


class Tombstones(Protocol):
    def is_tombstoned(self, subject_ref: str) -> bool: ...

    def record(self, subject_ref: str, *, saga_id: str) -> None: ...


class DeadLetters(Protocol):
    def send(self, body: dict[str, Any]) -> None: ...


class SqsDeadLetters:
    """Phase 3's halt signal: no compensation exists, a human takes over (§5)."""

    def __init__(self, queue_url: str, *, client: Any | None = None) -> None:
        self._queue_url = queue_url
        self._sqs = client or boto3.client("sqs")

    def send(self, body: dict[str, Any]) -> None:
        self._sqs.send_message(QueueUrl=self._queue_url, MessageBody=json.dumps(body))


@dataclass(frozen=True)
class SagaDeps:
    participants: ParticipantInvoker
    ledger: Ledger
    scheduler: Scheduler
    tombstones: Tombstones
    dead_letters: DeadLetters
    signer: ManifestSigner
    tokens: TokenMinter
    trusted_key_arns: frozenset[str] | None
    now: Callable[[], datetime]
    approval_timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    #: (T+7 delay, T+30 delay) in seconds. Integration shortens these; the *sequence*
    #: is never shortened — both sweeps always run.
    sweep_delays_seconds: tuple[int, int] = DEFAULT_SWEEP_DELAYS_SECONDS
    #: `None` uses the manifest's graceWindowDays; an integer overrides it for dev
    #: stages so `make integration` does not wait 30 days. Zero skips the pause.
    grace_seconds_override: int | None = None
    #: Bounded forward-recovery retries per participant in phase 3, then the DLQ.
    hard_delete_attempts: int = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def production_deps() -> SagaDeps:
    """Wire the real clients from the Lambda environment. Fails loudly on gaps —
    a missing table name must never degrade into an in-memory stand-in (ADR-017)."""
    stage = os.environ["PII_ERASURE_STAGE"]
    sweep_raw = os.environ.get("SWEEP_DELAYS_SECONDS", "")
    if sweep_raw:
        t7, t30 = (int(part) for part in sweep_raw.split(","))
        sweep_delays = (t7, t30)
    else:
        sweep_delays = DEFAULT_SWEEP_DELAYS_SECONDS
    grace_raw = os.environ.get("GRACE_SECONDS_OVERRIDE", "")
    trusted = os.environ.get("TRUSTED_SIGNING_KEY_ARNS", "")

    return SagaDeps(
        participants=LambdaParticipantInvoker(
            function_prefix=f"asdp-{stage}-",
        ),
        ledger=LedgerWriter(os.environ["LEDGER_TABLE"]),
        scheduler=_production_scheduler(stage),
        tombstones=TombstoneRegistry(os.environ["TOMBSTONES_TABLE"]),
        dead_letters=SqsDeadLetters(os.environ["SAGA_DLQ_URL"]),
        signer=ManifestSigner(os.environ["SIGNING_KEY_ARN"]),
        tokens=TokenMinter(os.environ["SIGNING_KEY_ARN"]),
        trusted_key_arns=frozenset(trusted.split(",")) if trusted else None,
        now=_utcnow,
        approval_timeout_seconds=int(
            os.environ.get("APPROVAL_TIMEOUT_SECONDS", str(DEFAULT_APPROVAL_TIMEOUT_SECONDS))
        ),
        sweep_delays_seconds=sweep_delays,
        grace_seconds_override=int(grace_raw) if grace_raw else None,
    )


def _production_scheduler(stage: str) -> Scheduler:
    return EventBridgeScheduler(
        stage=stage,
        target_arn=os.environ["RESUME_FUNCTION_ARN"],
        role_arn=os.environ["SCHEDULER_ROLE_ARN"],
        dlq_arn=os.environ.get("SAGA_DLQ_ARN") or None,
    )
