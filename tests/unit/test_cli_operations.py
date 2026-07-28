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
