"""The saga graph, end to end, hermetically — fakes for AWS, `InMemorySaver` for the
checkpointer, the REAL graph wiring for everything else.

The deployed gate (`make integration`) replays the same scenarios against real
services; these tests exist so a routing or binding defect fails in milliseconds in
`make check` rather than minutes into a deploy. The fakes implement the *contract*
(real response models, validated), never service behaviour — service behaviour is
exactly what fakes cannot testify about (V8-9's lesson).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pii_erasure.approval.tokens import ApprovalTokenError
from pii_erasure.contract import (
    Artifact,
    Deletability,
    DiscoverResponse,
    DiscoveryEvidence,
    Hold,
    MutationResponse,
    Outcome,
    ReceiptEvidence,
    Residual,
    VerifyResponse,
)
from pii_erasure.contract.registry import get as registry_get
from pii_erasure.manifest import Manifest, SignatureBlock, assert_digest
from pii_erasure.manifest.signing import SigningError
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.graph import build_graph
from pii_erasure.saga.invoker import ParticipantCallError
from pii_erasure.saga.nodes._shared import elapsed_seconds, iso, parse_iso
from pii_erasure.saga.nodes.hard_delete import make_hard_delete
from pii_erasure.saga.nodes.intake import make_intake
from pii_erasure.saga.nodes.sweep import STATUS_RESURRECTION
from pii_erasure.saga.nodes.verify import make_verify
from pii_erasure.saga.state import (
    STATUS_ABORTED,
    STATUS_ALREADY_TOMBSTONED,
    STATUS_BLOCKED,
    STATUS_COMPENSATED,
    STATUS_COMPLETED,
    set_once,
)
from tests.conftest import build_fixture_manifest

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
_TS = "2026-07-26T12:00:00Z"
_ZERO = "sha256:" + "0" * 64


# ─── contract-shaped fakes ────────────────────────────────────────────────────────────


class FakeParticipants:
    """Answers every verb with a valid contract response; failures are injected per
    (system_id, tool). Records every call so ordering is assertable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.failures: dict[tuple[str, str], Exception] = {}
        self.hold_on_discover: dict[str, Hold] = {}
        #: What `hard_delete` discloses it could not action — a PARTIAL outcome. The
        #: scoped-hold shape (ADR-027): the participant deletes what it may and names
        #: what it kept.
        self.residual_on_hard_delete: dict[str, tuple[Residual, ...]] = {}
        #: What `verify` still finds. Mutable between resumes on purpose — that is how
        #: a sweep at T+7 sees a world the T+0 verify did not.
        self.remaining_on_verify: dict[str, tuple[Artifact, ...]] = {}

    def call(self, system_id: str, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((system_id, tool, payload))
        failure = self.failures.get((system_id, tool))
        if failure is not None:
            raise failure

        if tool == "discover":
            hold = self.hold_on_discover.get(system_id)
            return DiscoverResponse(
                system_id=system_id,
                archetype=registry_get(system_id).archetype,
                found=False,
                deletability=(Deletability.BLOCKED_BY_HOLD if hold else Deletability.NOT_PRESENT),
                evidence=DiscoveryEvidence(query_digest=_ZERO, observed_at=_TS),
                holds=(hold,) if hold else (),
            ).digested_body()
        if tool == "verify":
            remaining = self.remaining_on_verify.get(system_id, ())
            return VerifyResponse(
                system_id=system_id,
                clean=not remaining,
                remaining=remaining,
                evidence=DiscoveryEvidence(query_digest=_ZERO, observed_at=_TS),
            ).digested_body()
        if tool in ("soft_delete", "restore", "hard_delete"):
            residual = (
                self.residual_on_hard_delete.get(system_id, ()) if tool == "hard_delete" else ()
            )
            return MutationResponse(
                system_id=system_id,
                outcome=Outcome.PARTIAL if residual else Outcome.APPLIED,
                affected=1,
                residual=residual,
                evidence=ReceiptEvidence(receipt_digest=_ZERO, applied_at=_TS),
                restore_token=f"rt-{system_id}" if tool == "soft_delete" else None,
            ).digested_body()
        raise AssertionError(f"unexpected tool {tool}")

    def tools_called(self, tool: str) -> list[str]:
        return [sid for sid, called, _ in self.calls if called == tool]


class FakeLedger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def append(self, *, saga_id: str, event_type: str, body: dict[str, Any]) -> None:
        self.entries.append((event_type, body))

    def events(self) -> list[str]:
        return [event for event, _ in self.entries]


class FakeScheduler:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def schedule_resume(self, request: Any) -> str:
        self.requests.append(request)
        return f"fake-{request.reason}"

    def reasons(self) -> list[str]:
        return [r.reason for r in self.requests]


class FakeTombstones:
    def __init__(self) -> None:
        self.recorded: set[str] = set()

    def is_tombstoned(self, subject_ref: str) -> bool:
        return subject_ref in self.recorded

    def record(self, subject_ref: str, *, saga_id: str) -> None:
        self.recorded.add(subject_ref)


class FakeDeadLetters:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send(self, body: dict[str, Any]) -> None:
        self.messages.append(body)


class FakeSigner:
    """Real digest discipline, fake cryptography — the digest checks are the logic
    under test here; the KMS signature itself is the deployed gate's business."""

    def sign(self, manifest: Manifest) -> Manifest:
        if manifest.signature is not None:
            raise SigningError("already signed")
        assert_digest(manifest)
        return manifest.model_copy(
            update={
                "signature": SignatureBlock(
                    kms_key_arn="arn:aws:kms:us-test-1:000000000000:key/fake",
                    value=base64.b64encode(b"fake-signature").decode(),
                )
            }
        )

    def verify(self, manifest: Manifest) -> None:
        if manifest.signature is None:
            raise SigningError("unsigned")
        assert_digest(manifest)


class FakeTokens:
    """Digest binding with real semantics, no KMS."""

    def mint(self, *, manifest_digest: str, saga_id: str, approver: str, approved_at: str) -> str:
        claim = {"manifestDigest": manifest_digest, "sagaId": saga_id, "approver": approver}
        return base64.b64encode(json.dumps(claim).encode()).decode()

    def verify(self, token: str, *, expected_digest: str, saga_id: str) -> dict[str, Any]:
        claim: dict[str, Any] = json.loads(base64.b64decode(token))
        if claim.get("manifestDigest") != expected_digest:
            raise ApprovalTokenError("token bound to a different digest")
        if claim.get("sagaId") != saga_id:
            raise ApprovalTokenError("token minted for a different saga")
        return claim


# ─── rig ──────────────────────────────────────────────────────────────────────────────


class Rig:
    def __init__(self, *, grace_override: int | None = None) -> None:
        #: Advanceable, because the duration metrics measure wall-clock time across a
        #: pause: `rig.clock = _NOW + timedelta(days=6)` before resuming is what a real
        #: six-day approval looks like from the node's side. Every existing test leaves
        #: it alone and so still sees a frozen clock.
        self.clock = _NOW
        self.participants = FakeParticipants()
        self.ledger = FakeLedger()
        self.scheduler = FakeScheduler()
        self.tombstones = FakeTombstones()
        self.dlq = FakeDeadLetters()
        self.deps = SagaDeps(
            participants=self.participants,
            ledger=self.ledger,
            scheduler=self.scheduler,
            tombstones=self.tombstones,
            dead_letters=self.dlq,
            signer=FakeSigner(),  # type: ignore[arg-type]
            tokens=FakeTokens(),  # type: ignore[arg-type]
            trusted_key_arns=None,
            now=lambda: self.clock,
            approval_timeout_seconds=3600,
            sweep_delays_seconds=(60, 120),
            grace_seconds_override=grace_override,
            hard_delete_attempts=2,
        )
        self.graph = build_graph(self.deps, InMemorySaver())

    def start(self, *, grace_days: int = 0, legal_holds: tuple[Hold, ...] = ()) -> dict[str, Any]:
        self.saga_id = "saga_unit_0001"
        self.subject_ref = "sub_unit_fixture"
        manifest = build_fixture_manifest(
            saga_id=self.saga_id,
            subject_ref=self.subject_ref,
            grace_window_days=grace_days,
            legal_holds=legal_holds,
        )
        self.config = {"configurable": {"thread_id": self.saga_id}}
        return dict(
            self.graph.invoke(
                {
                    "saga_id": self.saga_id,
                    "subject_ref": self.subject_ref,
                    "request_id": "dsr_unit",
                    "tenant_id": "meridian",
                    "provided_manifest": manifest.model_dump(mode="json", by_alias=True),
                },
                self.config,
                durability="sync",
            )
        )

    def resume(self, value: Any) -> dict[str, Any]:
        return dict(self.graph.invoke(Command(resume=value), self.config, durability="sync"))

    def approve(self, paused: dict[str, Any]) -> dict[str, Any]:
        digest = paused["__interrupt__"][0].value["manifestDigest"]
        self.digest = digest
        return self.resume({"decision": "approve", "digest": digest, "approver": "unit-approver"})


def _run_happy_path_to_completion(rig: Rig) -> dict[str, Any]:
    paused = rig.start()
    assert paused["__interrupt__"][0].value["gate"] == "approval"
    result = rig.approve(paused)
    # grace 0 is skipped; next pause is the T+7 sweep
    assert result["__interrupt__"][0].value["gate"] == "sweep"
    result = rig.resume({"wake_reason": "sweep_t7"})
    assert result["__interrupt__"][0].value["gate"] == "sweep"
    result = rig.resume({"wake_reason": "sweep_t30"})
    return result


# ─── the arcs ─────────────────────────────────────────────────────────────────────────


def test_happy_path_pauses_at_approval_and_completes_after_sweeps() -> None:
    rig = Rig()
    result = _run_happy_path_to_completion(rig)

    assert result["status"] == STATUS_COMPLETED
    assert rig.tombstones.recorded == {"sub_unit_fixture"}
    assert rig.dlq.messages == []
    soft = rig.participants.tools_called("soft_delete")
    hard = rig.participants.tools_called("hard_delete")
    assert len(soft) == 8
    assert len(hard) == 8
    for event in (
        "SAGA_STARTED",
        "MANIFEST_SIGNED",
        "APPROVAL_GRANTED",
        "TOMBSTONED",
        "VERIFIED_CLEAN",
        "SAGA_COMPLETED",
    ):
        assert event in rig.ledger.events(), f"missing ledger event {event}"
    assert rig.participants.tools_called("restore") == []


def test_phase2_revokes_identity_first_and_phase3_orders_derived_to_shred() -> None:
    rig = Rig()
    _run_happy_path_to_completion(rig)

    soft = rig.participants.tools_called("soft_delete")
    assert soft[0] == "cognito-identity", "revoke-first (§5.2) — identity leads phase 2"

    hard = rig.participants.tools_called("hard_delete")
    assert hard[0] == "vector-index", "derived stores go first, join key still alive"
    assert hard[-1] == "compliance-archive", "crypto-shred is dead last"
    assert hard.index("analytics-lake") < hard.index("billing-ledger")


def test_scheduler_receives_the_three_timer_kinds() -> None:
    rig = Rig()
    _run_happy_path_to_completion(rig)
    assert "approval_timeout" in rig.scheduler.reasons()
    assert "sweep_t7" in rig.scheduler.reasons()
    assert "sweep_t30" in rig.scheduler.reasons()


def test_phase2_failure_compensates_in_reverse_order() -> None:
    rig = Rig()
    # billing-ledger fails after cognito + profile-store were soft-deleted
    rig.participants.failures[("billing-ledger", "soft_delete")] = ParticipantCallError("boom")
    result = rig.start()

    assert result["status"] == STATUS_COMPENSATED
    soft = rig.participants.tools_called("soft_delete")
    restored = rig.participants.tools_called("restore")
    # Only what was actually soft-deleted is restored (billing failed, so it holds no
    # restore token), in exact reverse order: identity (revoked first) comes back last.
    assert soft[-1] == "billing-ledger"
    assert restored == list(reversed(soft[:-1]))
    assert restored[-1] == "cognito-identity"
    assert rig.participants.tools_called("hard_delete") == []
    assert "SAGA_COMPENSATED" in rig.ledger.events()


def test_approval_denial_compensates() -> None:
    rig = Rig()
    paused = rig.start()
    result = rig.resume({"decision": "deny", "approver": "unit-approver"})
    assert result["status"] == STATUS_COMPENSATED
    assert rig.participants.tools_called("hard_delete") == []
    assert "APPROVAL_DENIED" in rig.ledger.events()
    assert paused["__interrupt__"][0].value["manifestDigest"]  # the digest was offered


def test_approval_timeout_is_a_denial() -> None:
    rig = Rig()
    rig.start()
    result = rig.resume({"decision": "timeout", "approver": "scheduler"})
    assert result["status"] == STATUS_COMPENSATED
    assert rig.participants.tools_called("hard_delete") == []


def test_approval_with_wrong_digest_unwinds_not_executes() -> None:
    rig = Rig()
    rig.start()
    result = rig.resume(
        {"decision": "approve", "digest": "sha256:" + "f" * 64, "approver": "unit-approver"}
    )
    assert result["status"] == STATUS_COMPENSATED
    assert rig.participants.tools_called("hard_delete") == []
    assert "APPROVAL_INVALID" in rig.ledger.events()


def test_a_subject_wide_hold_blocks_before_anything_mutates() -> None:
    """A court freezing everything still stops everything (ADR-027).

    The scope was `"all"` until ADR-027 made scopes mean something, at which point it
    matched only locators beginning with the letters "all" — i.e. nothing. That the test
    still passed under the old code is exactly the point: a subject-wide veto made every
    scope string equivalent, including a wrong one.
    """
    rig = Rig()
    hold = Hold(hold_id="hold-lit-001", authority="dc-court", scope="*", basis="Art.17(3)(e)")
    result = rig.start(legal_holds=(hold,))
    assert result["status"] == STATUS_BLOCKED
    assert rig.participants.tools_called("soft_delete") == []
    assert rig.participants.tools_called("hard_delete") == []


def test_a_scoped_hold_does_not_stop_the_rest_of_the_estate() -> None:
    """ADR-027's decision, at the graph level. A hold over one table gives no lawful
    basis to retain the subject's uploads, so stopping everything over-retains — an
    under-deletion with no error attached."""
    rig = Rig()
    hold = Hold(
        hold_id="hold-lit-002",
        authority="dc-court",
        scope="public.invoices",
        basis="Art.17(3)(e)",
    )
    result = rig.start(legal_holds=(hold,))
    assert result["status"] != STATUS_BLOCKED, "a scoped hold vetoed the whole subject"
    assert rig.participants.tools_called("soft_delete"), "nothing was erased under a scoped hold"
    assert "HOLDS_SCOPED" in rig.ledger.events()


def test_a_scope_that_matches_nothing_is_recorded() -> None:
    """`scope: "all"` is a plausible thing to write and covers nothing. Under the old
    subject-wide veto that mistake was invisible; under ADR-027 it silently protects
    nothing, so the ledger names it rather than leaving it to be inferred."""
    rig = Rig()
    hold = Hold(hold_id="hold-typo", authority="dc-court", scope="all", basis="Art.17(3)(e)")
    rig.start(legal_holds=(hold,))
    body = next(body for event, body in rig.ledger.entries if event == "HOLDS_SCOPED")
    assert body["scopesMatchingNothing"] == ["all"]


def test_hold_appearing_at_recheck_blocks_phase3() -> None:
    rig = Rig()
    rig.participants.hold_on_discover["billing-ledger"] = Hold(
        hold_id="hold-new-001", authority="dc-court", scope="billing", basis="Art.17(3)(e)"
    )
    paused = rig.start()
    result = rig.approve(paused)
    assert result["status"] == STATUS_BLOCKED
    assert rig.participants.tools_called("hard_delete") == []
    assert rig.participants.tools_called("restore") == []
    assert "BLOCKED_BY_HOLD_AT_RECHECK" in rig.ledger.events()


def test_phase3_failure_goes_to_dlq_pauses_stuck_and_never_compensates() -> None:
    rig = Rig()
    rig.participants.failures[("billing-ledger", "hard_delete")] = ParticipantCallError("boom")
    paused = rig.start()
    result = rig.approve(paused)

    # Forward recovery ran out of road: DLQ raised, saga PAUSED at the stuck gate —
    # parked as data, waiting for a human (§5 "Stuck → manual remediation").
    stuck = result["__interrupt__"][0].value
    assert stuck["gate"] == "stuck"
    assert stuck["systemId"] == "billing-ledger"
    assert len(rig.dlq.messages) == 1
    assert rig.dlq.messages[0]["systemId"] == "billing-ledger"
    # THE invariant-6 assertion: a phase-3 failure triggers zero restore calls.
    assert rig.participants.tools_called("restore") == []
    assert "PHASE3_STUCK" in rig.ledger.events()
    # Everything BEFORE billing already committed its receipt — derived stores are
    # gone while the join key still exists, exactly the §5.2 ordering promise.
    done_before_stuck = rig.participants.tools_called("hard_delete")
    assert done_before_stuck.index("vector-index") < done_before_stuck.index("billing-ledger")
    # Retries happened before the DLQ: bounded forward recovery.
    billing_attempts = [
        1
        for sid, tool, _ in rig.participants.calls
        if sid == "billing-ledger" and tool == "hard_delete"
    ]
    assert len(billing_attempts) == rig.deps.hard_delete_attempts


def test_stuck_saga_resumes_after_remediation_with_zero_duplicate_applications() -> None:
    """The kill-mid-phase arc: stop at billing, remediate, resume — completed
    participants are skipped by their checkpointed receipts, so each system's
    HARD_DELETE_APPLIED appears exactly once."""
    rig = Rig()
    rig.participants.failures[("billing-ledger", "hard_delete")] = ParticipantCallError("boom")
    paused = rig.start()
    result = rig.approve(paused)
    assert result["__interrupt__"][0].value["gate"] == "stuck"

    del rig.participants.failures[("billing-ledger", "hard_delete")]  # remediation
    result = rig.resume({"wake_reason": "retry_phase3"})
    assert result["__interrupt__"][0].value["gate"] == "sweep"
    result = rig.resume({"wake_reason": "sweep_t7"})
    result = rig.resume({"wake_reason": "sweep_t30"})

    assert result["status"] == STATUS_COMPLETED
    applied = [body["systemId"] for e, body in rig.ledger.entries if e == "HARD_DELETE_APPLIED"]
    assert sorted(applied) == sorted(set(applied)), "a system was hard-deleted twice"
    assert len(applied) == 8
    assert rig.participants.tools_called("restore") == []
    # Billing was attempted (hard_delete_attempts=2) before the pause, then once
    # more after remediation — the retry is the node re-executing, receipts intact.
    billing_calls = [
        1
        for sid, tool, _ in rig.participants.calls
        if sid == "billing-ledger" and tool == "hard_delete"
    ]
    assert len(billing_calls) == rig.deps.hard_delete_attempts + 1


def test_revocation_during_grace_window_compensates() -> None:
    rig = Rig()
    paused = rig.start(grace_days=30)
    result = rig.approve(paused)
    assert result["__interrupt__"][0].value["gate"] == "grace_window"
    assert "grace_elapsed" in rig.scheduler.reasons()

    result = rig.resume({"wake_reason": "revoked"})
    assert result["status"] == STATUS_COMPENSATED
    assert rig.participants.tools_called("hard_delete") == []
    assert "REQUEST_REVOKED" in rig.ledger.events()


# ─── the dev grace override is a ceiling, not a replacement (V12-1) ───────────────────


def test_grace_override_never_lengthens_a_window_the_manifest_made_shorter() -> None:
    """The deployed dev configuration, which no hermetic test exercised until now.

    `GRACE_SECONDS_OVERRIDE=120` exists so a dev saga does not sit here for the
    manifest's 30 days (V11-1). Read as a *replacement* it also overrode a deliberate
    zero: every fixture manifest asks for `graceWindowDays: 0`, so on a dev stack the
    integration suite's sagas paused at a gate their assertions say is skipped — M5's
    deployed gate, silently invalidated by an M8 fix, with nothing to re-run it (V12-1).
    """
    rig = Rig(grace_override=120)
    paused = rig.start(grace_days=0)
    result = rig.approve(paused)

    assert result["__interrupt__"][0].value["gate"] == "sweep"
    assert "GRACE_WINDOW_SKIPPED" in rig.ledger.events()
    assert "grace_elapsed" not in rig.scheduler.reasons()


def test_grace_override_still_compresses_a_long_window() -> None:
    """The half V11-1 was written for: 30 days becomes 120 seconds, and the timer is
    real — compression by stack parameter, never by bypassing the scheduler."""
    rig = Rig(grace_override=120)
    paused = rig.start(grace_days=30)
    result = rig.approve(paused)

    assert result["__interrupt__"][0].value["gate"] == "grace_window"
    assert result["__interrupt__"][0].value["graceSeconds"] == 120
    scheduled = next(r for r in rig.scheduler.requests if r.reason == "grace_elapsed")
    assert scheduled.at == _NOW + timedelta(seconds=120)


# ─── a disclosed residual is not a failed erasure (ADR-027, V12-2) ────────────────────

#: `billing-ledger` retaining `public.invoices` under a litigation hold: the shape
#: ADR-027 made reachable, expressed the way the real participant expresses it.
_HELD_ROWS = Residual(
    kind="row",
    locator="public.invoices",
    count=3,
    reason="Retained under a live legal hold (GDPR Art. 17(3)(e)).",
)


def _rig_with_a_scoped_hold_residual() -> Rig:
    rig = Rig()
    rig.participants.residual_on_hard_delete["billing-ledger"] = (_HELD_ROWS,)
    rig.participants.remaining_on_verify["billing-ledger"] = (
        Artifact(kind="row", locator="public.invoices", count=3),
    )
    return rig


def test_a_residual_the_participant_disclosed_does_not_halt_the_saga() -> None:
    """ADR-027 lets phase 3 proceed past a scoped hold. The node that runs immediately
    afterwards then declared the result a failed erasure.

    `billing-ledger` is not registered `expects_residual` — its residue is not
    structural, it is one hold on one saga — so the T+0 verify graded lawful retention
    as unexpected residue, DLQ'd it and halted at `stuck`. ADR-027's "proceeds for the
    rest" was therefore unreachable end to end: the rule was written into `hold_recheck`
    and never extended to the two nodes downstream of it (V12-2).
    """
    rig = _rig_with_a_scoped_hold_residual()
    result = _run_happy_path_to_completion(rig)

    assert result["status"] == STATUS_COMPLETED
    assert "VERIFY_FOUND_RESIDUE" not in rig.ledger.events()
    assert "RESURRECTION_INCIDENT" not in rig.ledger.events()
    assert rig.dlq.messages == []
    assert "VERIFIED_CLEAN" in rig.ledger.events()


def test_more_rows_than_the_hold_retained_is_still_a_resurrection() -> None:
    """The disclosure is a *quantity*, not a licence. Five rows under a locator whose
    hold retained three is data that came back, and the count comparison is what keeps
    the exemption from becoming a blind spot for the held participant."""
    rig = _rig_with_a_scoped_hold_residual()
    paused = rig.start()
    result = rig.approve(paused)
    assert result["__interrupt__"][0].value["gate"] == "sweep"

    rig.participants.remaining_on_verify["billing-ledger"] = (
        Artifact(kind="row", locator="public.invoices", count=5),
    )
    result = rig.resume({"wake_reason": "sweep_t7"})

    assert result["status"] == STATUS_RESURRECTION
    assert "RESURRECTION_INCIDENT" in rig.ledger.events()
    assert [m["operation"] for m in rig.dlq.messages] == ["sweep_t7"]
    assert rig.participants.tools_called("restore") == []


def test_undisclosed_artifacts_at_the_first_sweep_are_a_resurrection_incident() -> None:
    """The plain case, which had no hermetic test at all: a participant that disclosed
    nothing reports artifacts at T+7. Distinct from a deletion failure — a *systemic*
    write path that bypasses the tombstone check — and it must never compensate."""
    rig = Rig()
    paused = rig.start()
    result = rig.approve(paused)
    assert result["__interrupt__"][0].value["gate"] == "sweep"

    rig.participants.remaining_on_verify["profile-store"] = (
        Artifact(kind="item", locator="profile-store:sub_unit_fixture", count=2),
    )
    result = rig.resume({"wake_reason": "sweep_t7"})

    assert result["status"] == STATUS_RESURRECTION
    incident = next(body for event, body in rig.ledger.entries if event == "RESURRECTION_INCIDENT")
    assert incident["sweep"] == "sweep_t7"
    assert [u["systemId"] for u in incident["unexpected"]] == ["profile-store"]
    assert rig.dlq.messages[0]["resurrection"][0]["systemId"] == "profile-store"
    assert rig.participants.tools_called("restore") == []
    assert "SAGA_COMPLETED" not in rig.ledger.events()


def test_tombstoned_subject_is_refused_at_intake() -> None:
    rig = Rig()
    rig.tombstones.recorded.add("sub_unit_fixture")
    result = rig.start()
    assert result["status"] == STATUS_ALREADY_TOMBSTONED
    assert rig.participants.calls == []
    assert "SAGA_REFUSED_TOMBSTONED" in rig.ledger.events()


def test_hard_delete_node_aborts_on_token_digest_mismatch_without_touching_participants() -> None:
    """The post-approval-mutation defence, at the node seam: a token bound to a
    different digest must abort before ANY participant is called (§8.3)."""
    rig = Rig()
    manifest = build_fixture_manifest(saga_id="saga_x", subject_ref="sub_x")
    signed = FakeSigner().sign(manifest)
    tokens = FakeTokens()
    node = make_hard_delete(rig.deps)

    state = {
        "saga_id": "saga_x",
        "subject_ref": "sub_x",
        "manifest": signed.model_dump(mode="json", by_alias=True),
        "manifest_digest": signed.digest,
        "approval": {
            "token": tokens.mint(
                manifest_digest="sha256:" + "e" * 64,  # bound to a DIFFERENT plan
                saga_id="saga_x",
                approver="unit",
                approved_at=_TS,
            )
        },
    }
    result = node(state)
    assert result["status"] == STATUS_ABORTED
    assert rig.participants.calls == []
    assert rig.tombstones.recorded == set()


# ─── the Lambda handler seams, with the graph patched in ─────────────────────────────


def _patched_handler(monkeypatch: pytest.MonkeyPatch, rig: Rig) -> Callable[..., dict[str, Any]]:
    import pii_erasure.saga.handler as handler_module

    monkeypatch.setattr(handler_module, "production_graph", lambda: rig.graph)
    return handler_module.lambda_handler


def test_handler_start_pauses_and_duplicate_start_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    handler = _patched_handler(monkeypatch, rig)
    manifest = build_fixture_manifest(saga_id="saga_h1", subject_ref="sub_h1")
    event = {
        "action": "start",
        "saga": {
            "saga_id": "saga_h1",
            "subject_ref": "sub_h1",
            "request_id": "dsr_h1",
            "tenant_id": "meridian",
            "manifest": manifest.model_dump(mode="json", by_alias=True),
        },
    }
    first = handler(event, None)
    assert first["status"] == "paused"
    assert first["gate"] == "approval"
    assert first["interrupt"]["manifestDigest"]

    second = handler(event, None)
    assert second["status"] == "already_started"


def test_handler_resume_refuses_a_thread_that_is_not_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig()
    handler = _patched_handler(monkeypatch, rig)
    result = handler({"action": "resume", "thread_id": "saga_missing", "resume": {}}, None)
    assert result["status"] == "not_paused"


def test_a_resume_shaped_for_another_gate_is_refused_without_wedging_the_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray resume must not poison a paused saga (V9-3).

    LangGraph persists a `Command(resume=…)` value against the pending interrupt
    *before* the node consumes it. So a wrong-shaped resume — a duplicate approval
    arriving after the saga has moved on to the sweep gate — does not merely fail
    once: the bad value is stored, and every subsequent LEGITIMATE resume replays it
    and fails identically. A live erasure request would be wedged past a statutory
    deadline by any caller that sent one stale payload.

    The executor therefore validates the resume against the current gate BEFORE
    delivering it, the same defence `scheduler/handler.py` applies to wake reasons.
    """
    rig = Rig()
    handler = _patched_handler(monkeypatch, rig)
    paused = rig.start()
    at_sweep = rig.approve(paused)
    assert at_sweep["__interrupt__"][0].value["gate"] == "sweep"

    stray = handler(
        {
            "action": "resume",
            "thread_id": rig.saga_id,
            "resume": {"decision": "approve", "digest": rig.digest, "approver": "dup"},
        },
        None,
    )
    assert stray["status"] == "resume_rejected"
    assert stray["gate"] == "sweep"

    # The saga must still be resumable by the wake it is actually waiting for.
    resumed = handler(
        {"action": "resume", "thread_id": rig.saga_id, "resume": {"wake_reason": "sweep_t7"}},
        None,
    )
    assert resumed["status"] == "paused"
    final = handler(
        {"action": "resume", "thread_id": rig.saga_id, "resume": {"wake_reason": "sweep_t30"}},
        None,
    )
    assert final["status"] == STATUS_COMPLETED


def test_a_wake_shaped_resume_is_refused_at_the_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map is checked both ways: a scheduler-shaped payload is not an approval."""
    rig = Rig()
    handler = _patched_handler(monkeypatch, rig)
    paused = rig.start()
    assert paused["__interrupt__"][0].value["gate"] == "approval"

    stray = handler(
        {"action": "resume", "thread_id": rig.saga_id, "resume": {"wake_reason": "sweep_t7"}},
        None,
    )
    assert stray["status"] == "resume_rejected"
    assert rig.participants.tools_called("hard_delete") == []

    # Still approvable afterwards — the refusal cost the saga nothing.
    approved = rig.approve(paused)
    assert approved["__interrupt__"][0].value["gate"] == "sweep"


def test_an_unknown_gate_accepts_no_resume_at_all() -> None:
    """A pause someone adds without deciding what may resume it must default closed."""
    from pii_erasure.saga.handler import _answers_gate

    assert not _answers_gate("some_new_gate", {"decision": "approve"})
    assert not _answers_gate("", {"wake_reason": "sweep_t7"})
    assert not _answers_gate("approval", "a string, not a mapping")
    assert not _answers_gate("approval", None)
    assert _answers_gate("approval", {"decision": "deny"})
    assert _answers_gate("sweep", {"wake_reason": "sweep_t7"})


# ─── discovery that finds nothing (V11-4) ─────────────────────────────────────────────


def test_an_empty_discovery_is_a_terminal_state_not_a_crash() -> None:
    """A subject the controller holds no data on is a legitimate request, not an error.

    `Manifest` requires at least one participant — deliberately — so building one from an
    empty sweep raised a pydantic ValidationError *inside a graph node*. The saga died
    mid-graph, an asynchronous intake swallowed the traceback, and the operator watched a
    poll that never advanced. Art. 12(3) still owes that person an answer; the answer is
    "nothing to erase", and it needs a state rather than a stack trace.
    """
    from langgraph.graph import END

    from pii_erasure.saga.edges import PATH_MAPS, route_after_plan
    from pii_erasure.saga.state import STATUS_NO_DATA

    assert route_after_plan({"status": STATUS_NO_DATA}) == "nothing"
    assert route_after_plan({"status": "running"}) == "continue"
    assert PATH_MAPS["plan"]["nothing"] is END
    assert PATH_MAPS["plan"]["continue"] == "hold_check"


def test_no_data_is_distinct_from_completed() -> None:
    """A certificate must never imply deletions that did not happen. `completed` means
    the erasure ran; `no_data` means there was nothing to run."""
    from pii_erasure.saga.state import STATUS_COMPLETED, STATUS_NO_DATA

    assert STATUS_NO_DATA != STATUS_COMPLETED


# ─── the two duration metrics, and the clock they measure ─────────────────────────────


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[float]]:
    """Every metric the executor nodes publish, captured with its VALUE.

    Patched at the two seams the nodes emit through rather than read out of captured log
    lines, because the value is the entire risk in this pair: a duration of zero is
    indistinguishable from a decision taken instantly, and it is the number an alarm
    threshold is compared against.
    """
    recorded: dict[str, list[float]] = {}

    def record(name: str, value: float, dimensions: Any, **context: Any) -> None:
        recorded.setdefault(name, []).append(value)

    monkeypatch.setattr("pii_erasure.saga.nodes._shared.emit", record)
    monkeypatch.setattr("pii_erasure.saga.nodes.verify.emit", record)
    return recorded


def test_the_approval_gate_measures_the_pause_and_not_the_resume(
    metrics: dict[str, list[float]],
) -> None:
    """Six days pass between the interrupt and the decision; the metric must say six days.

    This is why `started_at` is in the checkpointed state at all. `interrupt()`
    re-executes its node from the top on resume, so anything the approval gate learns
    from a local `now()` describes the moment the approver answered — the metric would
    read as a few milliseconds no matter how long the human took, which is the shape of
    an SLO that can never be breached.
    """
    rig = Rig()
    paused = rig.start()
    assert paused["__interrupt__"][0].value["gate"] == "approval"

    rig.clock = _NOW + timedelta(days=6)
    rig.approve(paused)

    assert metrics["approval.time_to_decision"] == [6 * 86400]


def test_a_denial_is_also_a_decision_and_is_measured(metrics: dict[str, list[float]]) -> None:
    """§10.1 asks for time to *decision*. Emitting only on approval would make the
    histogram a histogram of approvals, and the approval-timeout path — which is where a
    27-day silence actually shows up — would be the one case never measured."""
    rig = Rig()
    rig.start()
    rig.clock = _NOW + timedelta(days=14)
    result = rig.resume({"decision": "deny", "approver": "unit-approver"})

    assert result["status"] == STATUS_COMPENSATED
    assert metrics["approval.time_to_decision"] == [14 * 86400]


def test_an_invalid_resume_is_not_a_decision_and_is_not_measured(
    metrics: dict[str, list[float]],
) -> None:
    """A resume carrying the wrong digest is a TOCTOU signal, not an answer. Counting it
    would put a data point on the histogram for a decision nobody made."""
    rig = Rig()
    rig.start()
    rig.clock = _NOW + timedelta(days=3)
    rig.resume({"decision": "approve", "digest": _ZERO, "approver": "attacker"})

    assert "approval.time_to_decision" not in metrics


def test_saga_duration_stops_at_verification_not_at_the_graphs_end(
    metrics: dict[str, list[float]],
) -> None:
    """The measurement point is the whole design decision, so it gets a test.

    `sweep` is where `status` becomes `completed`, and it gets there after the T+30
    re-verification. A duration measured at END would therefore be a month longer than
    the answer took on every healthy saga, and any deadline-shaped threshold would fire
    on all of them. Ten days here, forty by the time the graph ends: the metric must say
    ten.
    """
    rig = Rig()
    paused = rig.start()
    rig.clock = _NOW + timedelta(days=10)
    result = rig.approve(paused)  # grace 0 → runs phase 3 and verify in this invocation
    assert result["__interrupt__"][0].value["gate"] == "sweep"

    rig.clock = _NOW + timedelta(days=17)
    rig.resume({"wake_reason": "sweep_t7"})
    rig.clock = _NOW + timedelta(days=40)
    final = rig.resume({"wake_reason": "sweep_t30"})

    assert final["status"] == STATUS_COMPLETED
    assert metrics["saga.duration"] == [10 * 86400]


def test_a_saga_that_predates_started_at_publishes_no_duration(
    metrics: dict[str, list[float]],
) -> None:
    """The compatibility path, at a real call site.

    A saga paused at the approval gate right now was checkpointed before `started_at`
    existed. On resume its state simply lacks the field, and the honest reading is *no
    data point* — never zero, which would report an erasure that took no time at all and
    would drag a p99 down with it. The node's other metric still lands, which is what
    separates "skipped the duration" from "the node fell over".
    """
    rig = Rig()
    manifest = build_fixture_manifest(saga_id="saga_pre_change", subject_ref="sub_pre_change")
    verify = make_verify(rig.deps)

    result = verify({"manifest": manifest.model_dump(mode="json", by_alias=True)})

    assert result == {"receipts": result["receipts"]}  # clean verify, nothing else changed
    assert metrics["deletion.residual_artifacts"] == [0]
    assert "saga.duration" not in metrics


def test_intake_re_executing_reuses_the_start_moment_it_already_recorded() -> None:
    """`started_at` is `set_once`, and a second write with a *different* value raises.

    Every other write-once field replays identically because it is derived from a
    manifest or a participant. This one is derived from the clock, which a re-executed
    node cannot reproduce — so intake reads before it writes. Without that, a Lambda
    retry would fail the saga inside the reducer.
    """
    rig = Rig()
    intake = make_intake(rig.deps)
    start = {
        "saga_id": "saga_retry",
        "subject_ref": "sub_retry",
        "request_id": "dsr_retry",
        "tenant_id": "meridian",
    }

    first = intake(start)
    rig.clock = _NOW + timedelta(days=1)
    second = intake({**start, **first})

    assert first["started_at"] == second["started_at"] == _TS
    assert set_once(first["started_at"], second["started_at"]) == _TS


def test_elapsed_is_absent_rather_than_zero_and_never_negative() -> None:
    """Two ways the helper could lie, both pinned.

    Absence must not become zero (see the compatibility test above), and clock skew
    between two Lambda invocations must not become a negative duration — CloudWatch
    cannot hold one, and it would silently poison the statistic for the window.
    """
    assert elapsed_seconds({}, _NOW) is None
    assert elapsed_seconds({"started_at": None}, _NOW) is None
    assert elapsed_seconds({"started_at": _TS}, _NOW + timedelta(hours=2)) == 7200
    assert elapsed_seconds({"started_at": _TS}, _NOW - timedelta(seconds=5)) == 0.0


def test_the_start_moment_round_trips_through_its_own_serialisation() -> None:
    """`iso` writes a trailing `Z`; `fromisoformat` only accepts one from 3.11, and
    `requires-python` is 3.10. A parser that agreed with itself but not with the writer
    would raise inside a node."""
    assert parse_iso(iso(_NOW)) == _NOW
    # An offset-less value is read as UTC — the platform writes nothing else.
    assert parse_iso("2026-07-26T12:00:00") == _NOW
