"""The operator commands' logic, with AWS faked out.

These are the parts of `cli/operations.py` that decide something, as opposed to the parts
that call boto3. Two of them are controls rather than conveniences:

* **`resume` refuses the approval gate.** A manual resume of an approval interrupt would
  deliver a decision with no digest and no authenticated approver — invariant 3 bypassed
  by an operator convenience command, which is precisely how a control dies.
* **`operator_token` has no fallback.** Approval must traverse the authenticated API, so
  absent credentials fail loudly. A fallback that invoked the Lambda directly would make
  every walkthrough green over a control nobody exercised.

The third is quieter and would bite in an audit: ledger chains are **per saga**, so
verification must be per saga. Running `verify_chain` over several sagas' entries at once
fails at the boundary between them and reports tampering that did not happen — and a
false alarm is how an audit tool stops being read.
"""

from __future__ import annotations

from typing import Any

import pytest

from pii_erasure.cli import operations
from pii_erasure.cli.operations import OperationError
from pii_erasure.ledger.chain import GENESIS_DIGEST, make_entry


@pytest.fixture(autouse=True)
def _stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_ERASURE_STAGE", "test")


# ─── resume never touches the approval gate ───────────────────────────────────────────


def test_resume_refuses_a_thread_at_the_approval_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole HITL control, bypassable by a convenience command, if this were absent."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        operations,
        "invoke_saga",
        lambda payload: (calls.append(payload), {"gate": "approval", "status": "paused"})[1],
    )
    with pytest.raises(OperationError) as raised:
        operations.resume_thread("saga_1")
    assert "invariant 3" in str(raised.value)
    assert [call["action"] for call in calls] == ["describe"], "a resume reached the graph"


def test_resume_sends_the_wake_the_gate_is_waiting_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the gate, then answer *that* gate. A resume shaped for a different one is
    persisted against the pending interrupt and wedges the thread (V9-3)."""
    calls: list[dict[str, Any]] = []

    def fake(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if payload["action"] == "describe":
            return {"gate": "grace_window", "status": "paused"}
        return {"status": "resumed"}

    monkeypatch.setattr(operations, "invoke_saga", fake)
    operations.resume_thread("saga_1")
    resume = next(call for call in calls if call["action"] == "resume")
    assert resume["resume"] == {"wake_reason": "grace_window"}


def test_resume_refuses_a_thread_that_is_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operations, "invoke_saga", lambda payload: {"gate": None, "status": "completed"}
    )
    with pytest.raises(OperationError, match="not paused"):
        operations.resume_thread("saga_1")


# ─── the approval path has no bypass ──────────────────────────────────────────────────


def test_missing_operator_credentials_fail_with_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PII_ERASURE_OPERATOR_USER", raising=False)
    monkeypatch.delenv("PII_ERASURE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        operations,
        "outputs",
        lambda *stacks: {"OperatorPoolId": "pool-1", "OperatorClientId": "client-1"},
    )
    with pytest.raises(OperationError) as raised:
        operations.operator_token()
    message = str(raised.value)
    assert "admin-create-user" in message, "the failure must be actionable, not just loud"
    assert "asdp-approvers" in message
    assert "no bypass" in message


def test_a_non_https_endpoint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL comes from a CloudFormation output, which is exactly what every SSRF
    post-mortem calls trusted."""
    monkeypatch.setattr(
        operations, "outputs", lambda *stacks: {"OperatorApiUrl": "file:///etc/passwd"}
    )
    monkeypatch.setattr(operations, "operator_token", lambda: "token")
    with pytest.raises(OperationError, match="non-https"):
        operations.api_call("GET", "/threads")


def test_approving_without_a_manifest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A saga not at the approval gate has no digest to echo, and an approval with no
    digest is invariant 3 waived."""
    monkeypatch.setattr(
        operations,
        "api_call",
        lambda method, path, body=None: {"review": None, "gate": None, "status": "running"},
    )
    with pytest.raises(OperationError, match="no signed manifest"):
        operations.submit_decision("saga_1", "approve")


def test_an_approval_echoes_the_digest_that_was_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What makes a stale screen fail: the digest sent back is the one just rendered, and
    the API compares it against what is pending (§8.3)."""
    sent: list[dict[str, Any]] = []

    def fake(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "GET":
            return {"review": {"manifestDigest": "sha256:live"}}
        sent.append(body or {})
        return {"status": "resumed"}

    monkeypatch.setattr(operations, "api_call", fake)
    operations.submit_decision("saga_1", "approve")
    assert sent == [{"decision": "approve", "manifestDigest": "sha256:live"}]


def test_a_denial_carries_no_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def fake(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "GET":
            return {"review": {"manifestDigest": "sha256:live"}}
        sent.append(body or {})
        return {"status": "resumed"}

    monkeypatch.setattr(operations, "api_call", fake)
    operations.submit_decision("saga_1", "deny")
    assert sent == [{"decision": "deny"}]


# ─── ledger verification is per saga ──────────────────────────────────────────────────


def _chain(saga_id: str, count: int) -> list[Any]:
    entries = []
    prev = GENESIS_DIGEST
    for seq in range(count):
        entry = make_entry(
            saga_id=saga_id,
            seq=seq,
            event_type=f"event_{seq}",
            at=f"2026-07-28T00:00:0{seq}Z",
            body={"subject_ref": "sub_1"},
            prev_digest=prev,
        )
        prev = entry.digest
        entries.append(entry)
    return entries


def test_two_sagas_verify_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concatenating them and verifying once would fail at the boundary and report
    tampering that did not happen — a false alarm in an audit tool is how the tool stops
    being read."""
    entries = _chain("saga_a", 3) + _chain("saga_b", 2)
    monkeypatch.setattr(operations, "ledger_entries", lambda saga_id=None: entries)
    verified, returned = operations.verify_ledger()
    assert verified == 5
    assert len(returned) == 5


def test_a_tampered_entry_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction — per-saga verification must not have become no verification."""
    from dataclasses import replace

    from pii_erasure.ledger.chain import ChainError

    entries = _chain("saga_a", 3)
    entries[1] = replace(entries[1], body={"subject_ref": "sub_TAMPERED"})
    monkeypatch.setattr(operations, "ledger_entries", lambda saga_id=None: entries)
    with pytest.raises(ChainError):
        operations.verify_ledger()


def test_the_scan_projects_the_attribute_the_table_actually_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make ledger` with no `--saga` scans for the sagas the table holds, and had never
    run: it projected `sagaId` when the attribute is `saga_id`, so DynamoDB returned an
    empty item per row and the first one raised `KeyError` (V12-4).

    Nothing caught it because every other test here monkeypatches `ledger_entries`
    wholesale — the seam with the defect was the seam the tests replaced. So this one
    fakes the *client*, one layer lower, and builds its rows with the writer's own
    serialiser so the attribute name cannot drift between the two files again.
    """
    from pii_erasure.ledger.writer import _to_item

    rows = [_to_item(_chain(saga, 1)[0]) for saga in ("saga_a", "saga_b")]
    projected: list[str] = []

    class _FakePaginator:
        def paginate(self, **kwargs: Any) -> Any:
            projected.append(kwargs["ProjectionExpression"])
            keys = {kwargs["ProjectionExpression"]}
            yield {"Items": [{k: v for k, v in row.items() if k in keys} for row in rows]}

    class _FakeDdb:
        def get_paginator(self, name: str) -> Any:
            assert name == "scan"
            return _FakePaginator()

        def query(self, **kwargs: Any) -> Any:
            wanted = kwargs["ExpressionAttributeValues"][":sid"]["S"]
            return {"Items": [r for r in rows if r["saga_id"]["S"] == wanted]}

    monkeypatch.setattr(operations, "outputs", lambda *_: {"LedgerTable": "asdp-test-ledger"})
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: _FakeDdb())

    found = operations.ledger_entries()

    assert projected == ["saga_id"]
    assert sorted(e.saga_id for e in found) == ["saga_a", "saga_b"]


def test_entries_are_ordered_before_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan returns rows in whatever order DynamoDB likes; a chain verified out of
    order fails on sequence, not on tampering."""
    entries = _chain("saga_a", 4)
    monkeypatch.setattr(operations, "ledger_entries", lambda saga_id=None: list(reversed(entries)))
    verified, _ = operations.verify_ledger()
    assert verified == 4


def test_the_example_operator_cannot_reach_a_real_person() -> None:
    """The address in the copy-paste instructions is on a reserved TLD.

    Cognito needs a username that *parses* as an email and never checks the mailbox, so
    an example address here is a demo credential, not a contact. `.invalid` is reserved
    by RFC 6761 and cannot resolve — which matters because instructions get copied
    verbatim, and a plausible-looking real domain in a deletion tool's setup steps is one
    typo away from mailing a stranger.
    """
    assert operations.EXAMPLE_OPERATOR.endswith(".invalid")


def test_the_instructions_use_the_reserved_address_throughout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three commands, not just the first — a half-substituted example is worse than
    none, because the mismatch between --username values fails on the second step."""
    monkeypatch.delenv("PII_ERASURE_OPERATOR_USER", raising=False)
    monkeypatch.delenv("PII_ERASURE_OPERATOR_PASSWORD", raising=False)
    monkeypatch.setattr(
        operations,
        "outputs",
        lambda *stacks: {"OperatorPoolId": "pool-1", "OperatorClientId": "client-1"},
    )
    with pytest.raises(OperationError) as raised:
        operations.operator_token()
    message = str(raised.value)
    assert message.count(operations.EXAMPLE_OPERATOR) == 4, message
    assert "example.com" not in message


# ─── waiting on a saga that already halted ────────────────────────────────────────────


def test_a_halted_saga_ends_the_wait_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling a corpse for fifteen minutes is fifteen minutes in which the operator
    believes work is in progress, followed by "did not reach the gate" — true, useless,
    and late."""
    monkeypatch.setattr(
        operations,
        "describe_thread",
        lambda thread_id: {"status": "stuck", "gate": None, "errors": [{"where": "phase3"}]},
    )
    with pytest.raises(OperationError) as raised:
        operations.wait_for("saga_1", gate="approval")
    assert "stuck" in str(raised.value)
    assert "phase3" in str(raised.value), "the operator needs the error, not just the status"


def test_the_target_status_is_not_treated_as_a_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    """`completed` is terminal AND the thing the walkthrough waits for — a naive terminal
    check would turn every successful run into a failure."""
    monkeypatch.setattr(
        operations, "describe_thread", lambda thread_id: {"status": "completed", "gate": None}
    )
    assert operations.wait_for("saga_1", status="completed")["status"] == "completed"


def test_reaching_the_gate_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operations,
        "describe_thread",
        lambda thread_id: {"status": "paused", "gate": "approval"},
    )
    assert operations.wait_for("saga_1", gate="approval")["gate"] == "approval"


# ─── a terminal status must explain itself (V11-8) ────────────────────────────────────


def test_a_blocked_saga_explains_that_the_hold_worked(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "halted at status 'blocked'. Errors: none recorded" is accurate and tells an
    operator nothing — worse, "none recorded" reads as a swallowed error, when a blocked
    saga is the system refusing correctly and having nothing to apologise for."""
    monkeypatch.setattr(
        operations, "describe_thread", lambda thread_id: {"status": "blocked", "gate": None}
    )
    with pytest.raises(OperationError) as raised:
        operations.wait_for("saga_1", gate="approval")
    message = str(raised.value)
    assert "legal hold" in message
    assert "not a failure" in message
    assert "none recorded" not in message


@pytest.mark.parametrize("status", ["blocked", "no_data", "aborted", "stuck", "compensated"])
def test_every_terminal_status_has_an_explanation(status: str) -> None:
    """A status with no explanation is the one an operator will meet at 2am."""
    assert operations._TERMINAL_EXPLANATIONS.get(status), f"{status} explains nothing"


def test_the_terminal_set_and_the_explanations_agree() -> None:
    """Two lists that must not drift: a status in one and not the other is either an
    unexplained halt or an explanation for something that never halts."""
    assert set(operations._TERMINAL_EXPLANATIONS) <= operations.TERMINAL_STATUSES
    assert operations.TERMINAL_STATUSES - {"completed"} <= set(operations._TERMINAL_EXPLANATIONS)


def test_every_saga_status_is_classified() -> None:
    """The check the one above cannot make (V13-13).

    Comparing the CLI's two hand-maintained lists to *each other* catches drift between
    them and nothing else. `already_tombstoned` was absent from both, so they agreed —
    and a deployed walkthrough polled a saga that had halted at four seconds for the full
    fifteen minutes, then reported "did not reach 'approval' within 900s". True, useless,
    and fifteen minutes late, which is verbatim what `wait_for`'s own docstring promises
    not to do.

    The source of truth was never either list; it is `saga/state.py`. So the statuses are
    read from there, and every one must be *classified* — terminal or explicitly not.
    Adding a status to the saga now fails this test until someone decides which it is,
    rather than defaulting into a long poll that misdiagnoses itself as a timeout.
    """
    from pii_erasure.saga import state

    declared = {
        value
        for name, value in vars(state).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }
    classified = operations.TERMINAL_STATUSES | operations.NON_TERMINAL_STATUSES
    unclassified = declared - classified
    assert not unclassified, (
        f"{sorted(unclassified)} exist in saga/state.py and are neither in "
        f"TERMINAL_STATUSES nor NON_TERMINAL_STATUSES. A saga reaching one would be "
        f"polled until POLL_TIMEOUT_SECONDS and then reported as a timeout, which sends "
        f"the operator looking for slowness instead of for the halt that already happened."
    )


def test_the_two_classifications_do_not_overlap() -> None:
    """ "Terminal" and "still moving" are contradictory claims; a status in both would let
    `wait_for` take either branch depending on which check runs first."""
    overlap = operations.TERMINAL_STATUSES & operations.NON_TERMINAL_STATUSES
    assert not overlap, f"{sorted(overlap)} is classified as both terminal and non-terminal"


def test_an_already_tombstoned_saga_stops_the_wait_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioural half of V13-13, and the half that actually failed a run.

    Asserted through `wait_for` rather than against the constant, because membership in a
    frozenset is not the property that matters — ending the wait is.

    **The timeout is shortened first, and that is load-bearing.** Written against the real
    `POLL_TIMEOUT_SECONDS`, a regression here does not fail — it *polls for fifteen
    minutes*, exactly as the deployed run did, and `make check` looks hung rather than red.
    A test whose failure mode is an indefinite stall gets killed and skipped rather than
    read. Verified by removing the fix: this now goes red in about a second.
    """
    monkeypatch.setattr(operations, "POLL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(operations, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        operations,
        "describe_thread",
        lambda thread_id: {"status": "already_tombstoned", "gate": None},
    )
    with pytest.raises(OperationError, match="already been erased"):
        operations.wait_for("saga_test", gate="approval")


def test_recorded_errors_are_still_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explanation supplements the detail; it must not replace it."""
    monkeypatch.setattr(
        operations,
        "describe_thread",
        lambda thread_id: {"status": "stuck", "gate": None, "errors": [{"where": "phase3"}]},
    )
    with pytest.raises(OperationError, match="phase3"):
        operations.wait_for("saga_1", gate="approval")
