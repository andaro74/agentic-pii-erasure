"""M9's chaos cases — the failures that only exist between components.

**This costs money and takes ~20-30 minutes.** Every case drives a real saga through
real participants; the Aurora resume from 0 ACU and the Athena round-trips dominate.

PROJECT-STRUCTURE.md lists eight chaos and durability cases. Four of them were already
built as M5's deployed gate and live in `test_saga.py`, which is why they are *marked*
`chaos` there rather than reimplemented here — a second implementation of "phase 3 never
compensates" is a second thing to keep true, and the weaker of the two would rot:

| Case | Lives in |
|---|---|
| participant fails in phase 2 → full compensation | `test_saga.py` |
| participant fails in phase 3 → no compensation, DLQ, halt | `test_saga.py` |
| executor killed mid-phase → resume, zero duplicate calls | `test_saga.py` (same arc) |
| manifest mutated after approval → abort to re-approval | `test_saga.py` |
| **Scheduler fires twice → exactly one resume** | here |
| **hold appears during the grace window → scoped refusal** | here |
| **subject reappears at T+7 → resurrection incident** | here |
| upgrade canary → pause, bump both pins, resume | `test_upgrade_canary.py` (own gate) |

`make chaos` therefore collects seven of the eight; the canary is deliberately excluded
because it is a release gate that needs a `CANARY_STAGE` and a redeploy between its two
halves. Its exclusion is in the marker, not in a comment somebody has to remember.

The three cases here share a property worth stating: each is driven by a **real lever**,
not by patching the saga. A duplicate wake is a second real invocation of the real resume
Lambda; a hold is a real row in `public.legal_holds`; a resurrection is real data written
back through the same fixture writers that seeded it. Nothing is mocked — a mock standing
in for a participant would make the deployed gate hermetic, which is the one thing it
must not be.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from pii_erasure.contract.holds import blocks
from pii_erasure.ledger import verify_chain
from pii_erasure.participants._base.idempotency import STATUS_COMPLETED
from pii_erasure.saga.tombstone import subject_hash
from tests.conftest import build_fixture_manifest
from tests.integration.conftest import EXECUTOR, RESUME, STAGE
from tests.integration.test_saga import (
    _approve,
    _drain_dlq_for,
    _invoke,
    _ledger_events,
    _seeded_subject,
    _stack_output,
)

#: `chaos` only, deliberately. The four cases in `test_saga.py` carry BOTH markers —
#: they are M5's gate and chaos cases at once — but adding three more full arcs to
#: `make integration` would lengthen the gate that runs most often to buy nothing:
#: `make chaos` already runs them, and it runs `test_saga.py`'s four alongside.
pytestmark = pytest.mark.chaos

#: The resume handler's own partition in the idempotency table. Duplicated from
#: `scheduler/handler.py` deliberately: a test that imported the constant would pass if
#: the handler renamed its partition and stopped deduplicating what it used to.
RESUME_SYSTEM_ID = "saga-resume"

#: Enough participants to make "the hold covers one of them" a real statement, and few
#: enough that three arcs fit in a coffee break. `billing-ledger` is the only one of the
#: eight that reads a hold table, so it is not optional here.
_SUBSET = ("cognito-identity", "profile-store", "billing-ledger")


@pytest.fixture
def subject(rig: Any) -> Iterator[str]:
    with _seeded_subject(rig, _SUBSET) as handle:
        yield handle


def _start(lambda_client: Any, handle: str, saga_id: str) -> dict[str, Any]:
    manifest = build_fixture_manifest(saga_id=saga_id, subject_ref=handle, system_ids=_SUBSET)
    return _invoke(
        lambda_client,
        EXECUTOR,
        {
            "action": "start",
            "saga": {
                "saga_id": saga_id,
                "subject_ref": handle,
                "request_id": f"dsr_{saga_id}",
                "tenant_id": "meridian",
                "manifest": manifest.model_dump(mode="json", by_alias=True),
            },
        },
    )


def _describe(lambda_client: Any, saga_id: str) -> dict[str, Any]:
    return _invoke(lambda_client, EXECUTOR, {"action": "describe", "thread_id": saga_id})


def _wake(lambda_client: Any, saga_id: str, reason: str) -> dict[str, Any]:
    return _invoke(lambda_client, RESUME, {"thread_id": saga_id, "wake_reason": reason})


# ─── EventBridge Scheduler fires twice → exactly one resume ──────────────────────────


def test_a_duplicate_wake_never_becomes_a_second_resume(
    rig: Any,
    lambda_client: Any,
    subject: str,
) -> None:
    """Scheduler delivery is at-least-once, and a duplicate resume of a phase-3 node is
    a duplicate deletion attempt (invariant 11).

    Two independent defences guard this, and the test exercises them **separately**,
    because in normal operation the first one hides the second:

    1. **Stale-wake filtering** catches the ordinary duplicate. By the time a redelivered
       `sweep_t7` arrives the graph has advanced to an interrupt that expects `sweep_t30`,
       so the wake is dropped by inspection.
    2. **Delivery dedup** catches the duplicate the filter cannot see — one for a wake the
       current gate *does* still expect. Reproduced with a real prior claim in the real
       idempotency table, which is the state a first delivery leaves behind.

    Testing only (1) would leave (2) unexercised while looking like a passing duplicate
    test, and (2) is the defence that matters when the redelivery is fast rather than late.
    """
    saga_id = f"saga_chaos_{uuid.uuid4().hex[:12]}"
    started = _start(lambda_client, subject, saga_id)
    assert started["gate"] == "approval", started
    approved = _approve(lambda_client, saga_id, started["interrupt"]["manifestDigest"])
    assert approved["gate"] == "sweep", approved

    # ── 1. the ordinary duplicate: the graph has moved on ────────────────────
    first = _wake(lambda_client, saga_id, "sweep_t7")
    assert first["status"] == "paused", first
    replay = _wake(lambda_client, saga_id, "sweep_t7")
    assert replay["status"] == "stale_wake", replay

    swept = [
        entry.body["sweep"]
        for entry in _ledger_events(saga_id)
        if entry.event_type == "SWEEP_CLEAN"
    ]
    assert swept == ["sweep_t7"], f"the sweep ran {len(swept)} times: {swept}"

    # ── 2. the duplicate the filter cannot see ───────────────────────────────
    # A prior COMPLETED claim for a wake the gate still expects — exactly what a first
    # delivery leaves behind, planted so the second delivery arrives while the answer
    # is still the one the gate wants.
    ddb = boto3.client("dynamodb")
    key = f"{saga_id}\x1fsweep_t30"
    now = int(time.time())
    ddb.put_item(
        TableName=f"asdp-{STAGE}-idempotency",
        Item={
            "system_id": {"S": RESUME_SYSTEM_ID},
            "idempotency_key": {"S": key},
            "status": {"S": STATUS_COMPLETED},
            "claimed_at": {"N": str(now)},
            "expires_at": {"N": str(now + 3600)},
            "response": {"S": json.dumps({"status": "paused"})},
        },
    )
    try:
        deduped = _wake(lambda_client, saga_id, "sweep_t30")
        assert deduped["status"] == "duplicate_wake", deduped
        # The claim did not merely report — it prevented the graph from running.
        state = _describe(lambda_client, saga_id)
        assert state["status"] == "paused", state
        assert state["gate"] == "sweep", state
        assert "SAGA_COMPLETED" not in [e.event_type for e in _ledger_events(saga_id)]
    finally:
        ddb.delete_item(
            TableName=f"asdp-{STAGE}-idempotency",
            Key={"system_id": {"S": RESUME_SYSTEM_ID}, "idempotency_key": {"S": key}},
        )

    final = _wake(lambda_client, saga_id, "sweep_t30")
    assert final["status"] == "completed", final
    entries = _ledger_events(saga_id)
    assert verify_chain(entries) == len(entries)
    applied = [e.body["systemId"] for e in entries if e.event_type == "HARD_DELETE_APPLIED"]
    assert sorted(applied) == sorted(set(applied)), "a system was hard-deleted twice"


# ─── a hold appears during the grace window → it blocks its scope (ADR-027) ──────────

_HOLD_SCOPE = "public.invoices"


def _place_hold(rig: Any, handle: str, hold_id: str) -> None:
    _module, generator, _config = rig
    generator._sql(
        "INSERT INTO public.legal_holds (hold_id, subject_ref, authority, scope, basis) "
        "VALUES (:hold_id, :subject_ref, :authority, :scope, :basis) ON CONFLICT DO NOTHING",
        hold_id=hold_id,
        subject_ref=handle,
        authority="Chancery Division (fabricated)",
        scope=_HOLD_SCOPE,
        basis="GDPR Art.17(3)(e)",
    )


def _lift_hold(rig: Any, hold_id: str) -> None:
    _module, generator, _config = rig
    # `_cleanup` walks the participant's own `_DELETE_SQL`, which does not touch
    # `legal_holds` — nothing in the product deletes a hold, so the test owns this row.
    generator._sql("DELETE FROM public.legal_holds WHERE hold_id = :hold_id", hold_id=hold_id)


def test_a_hold_filed_after_approval_stops_its_scope_and_nothing_else(
    rig: Any,
    lambda_client: Any,
    subject: str,
) -> None:
    """ADR-027, end to end and against a real hold table.

    The plan is approved with no hold in existence. A litigation hold over
    `public.invoices` — and nothing else — is then filed, which is precisely the §5.3
    scenario: the approver authorised a plan the world has since changed underneath.

    Phase 3 re-reads holds live (`hold_recheck`) and proceeds for the participants the
    hold never named, while `billing-ledger` refuses its own scope and returns PARTIAL
    with a residual saying so. Safe in the direction that matters — the approver
    authorised deleting *more* than now happens, and deleting less than approved needs no
    fresh approval (invariant 3 is not in play).

    The corroboration that matters is from outside the saga: the held rows are still in
    Aurora, and the unheld ones are gone.
    """
    saga_id = f"saga_chaos_{uuid.uuid4().hex[:12]}"
    hold_id = f"hold_chaos_{uuid.uuid4().hex[:8]}"
    started = _start(lambda_client, subject, saga_id)
    assert started["gate"] == "approval", started
    # Planned and approved in a world with no hold: phase 1 recorded none.
    assert "BLOCKED_BY_HOLD" not in [e.event_type for e in _ledger_events(saga_id)]

    _place_hold(rig, subject, hold_id)
    try:
        approved = _approve(lambda_client, saga_id, started["interrupt"]["manifestDigest"])
        assert approved["status"] == "paused", approved
        assert approved["gate"] == "sweep", approved

        events = [e.event_type for e in _ledger_events(saga_id)]
        assert "HOLDS_SCOPED_AT_RECHECK" in events, (
            "a hold covering one participant must not halt the whole saga (ADR-027)"
        )
        assert "BLOCKED_BY_HOLD_AT_RECHECK" not in events

        by_system = {
            e.body["systemId"]: e.body
            for e in _ledger_events(saga_id)
            if e.event_type == "HARD_DELETE_APPLIED"
        }
        assert by_system["cognito-identity"]["outcome"] == "APPLIED"
        assert by_system["profile-store"]["outcome"] == "APPLIED"
        assert by_system["billing-ledger"]["outcome"] == "PARTIAL", (
            "a participant that kept rows under a hold must say so — invariant 7"
        )
        assert by_system["billing-ledger"]["residualCount"] >= 1

        # ── from outside the saga: the hold's scope survived, the rest did not ──
        counts = _billing_counts(rig, subject)
        assert counts["public.invoices"] > 0, "held rows must still be there"
        assert counts["public.customers"] == 0, "unheld rows must be gone"
        assert blocks(_holds_in_aurora(rig, subject), "public.invoices")

        # A disclosed residual is a disclosure, not a failure: the T+0 verify let the
        # saga through to the sweep gate rather than calling lawful retention residue.
        assert "VERIFY_FOUND_RESIDUE" not in events
        assert "VERIFIED_CLEAN" in events

        final = _wake(lambda_client, saga_id, "sweep_t7")
        assert final["status"] == "paused", final
        final = _wake(lambda_client, saga_id, "sweep_t30")
        assert final["status"] == "completed", final
        assert "RESURRECTION_INCIDENT" not in [e.event_type for e in _ledger_events(saga_id)], (
            "rows retained under a live hold are not a resurrection"
        )

        tombstone = boto3.client("dynamodb").get_item(
            TableName=f"asdp-{STAGE}-tombstones",
            Key={"subject_hash": {"S": subject_hash(subject)}},
            ConsistentRead=True,
        )
        assert "Item" in tombstone
    finally:
        _lift_hold(rig, hold_id)


def _billing_counts(rig: Any, handle: str) -> dict[str, int]:
    """Row counts straight from Aurora — the participant's own statements, run by the
    test rather than by the system under test."""
    from pii_erasure.participants.billing_ledger import handler as billing

    _module, generator, _config = rig
    counts: dict[str, int] = {}
    for table, sql in billing._COUNT_SQL.items():
        result = generator._sql(sql, subject_ref=handle)
        records = result.get("records") or [[{"longValue": 0}]]
        counts[table] = int(records[0][0].get("longValue", 0))
    return counts


def _holds_in_aurora(rig: Any, handle: str) -> tuple[Any, ...]:
    from pii_erasure.contract import Hold
    from pii_erasure.participants.billing_ledger import handler as billing

    _module, generator, _config = rig
    result = generator._sql(billing._HOLDS_SQL, subject_ref=handle)
    return tuple(
        Hold(
            hold_id=str(record[0].get("stringValue", "")),
            authority=str(record[1].get("stringValue", "")),
            scope=str(record[2].get("stringValue", "")),
            basis=str(record[3].get("stringValue", "")),
        )
        for record in result.get("records") or []
    )


# ─── the subject reappears at T+7 → a resurrection incident ──────────────────────────


def test_data_written_back_after_erasure_is_a_resurrection_not_a_deletion_failure(
    rig: Any,
    lambda_client: Any,
    subject: str,
) -> None:
    """Callum's case (§5.3): a nightly job with a stale copy re-creates the subject.

    The saga completed. Verify was clean. Then, between T+0 and the first sweep, rows
    come back — written by the same fixture writer that seeded them, because a
    resurrection is *ordinary writes to a system that forgot*, not a special API.

    This is a different incident from a failed deletion and is raised as one: its own
    ledger event, its own DLQ message, and no retry — retrying a hard delete would erase
    the rows and leave the write path that made them intact. Nothing is restored, and
    the saga does not report completion.
    """
    module, generator, _config = rig
    saga_id = f"saga_chaos_{uuid.uuid4().hex[:12]}"

    started = _start(lambda_client, subject, saga_id)
    approved = _approve(lambda_client, saga_id, started["interrupt"]["manifestDigest"])
    assert approved["gate"] == "sweep", approved
    assert "VERIFIED_CLEAN" in [e.event_type for e in _ledger_events(saga_id)]

    # The resurrection: the same writer, the same rows, after the erasure.
    generator._writers()["profile-store"](
        {
            "subjectRef": subject,
            "displayName": "Saga Integration Fixture",
            "email": f"{subject}@meridian.invalid",
        },
        module.PLACEMENTS["profile-store"],
    )

    result = _wake(lambda_client, saga_id, "sweep_t7")
    assert result["status"] == "resurrection_incident", result

    entries = _ledger_events(saga_id)
    assert verify_chain(entries) == len(entries)
    incident = next(e for e in entries if e.event_type == "RESURRECTION_INCIDENT")
    assert incident.body["sweep"] == "sweep_t7"
    assert [u["systemId"] for u in incident.body["unexpected"]] == ["profile-store"]
    assert "SAGA_COMPLETED" not in [e.event_type for e in entries]

    message = _drain_dlq_for(_stack_output("saga", "SagaDlqUrl"), saga_id)
    assert message["operation"] == "sweep_t7"
    assert message["resurrection"][0]["systemId"] == "profile-store"

    # No compensation, ever — and specifically no restore of the systems that *were*
    # erased. The tombstone stands: the subject is still erased, something else is broken.
    assert "SAGA_COMPENSATED" not in [e.event_type for e in entries]
    tombstone = boto3.client("dynamodb").get_item(
        TableName=f"asdp-{STAGE}-tombstones",
        Key={"subject_hash": {"S": subject_hash(subject)}},
        ConsistentRead=True,
    )
    assert "Item" in tombstone
