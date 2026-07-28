"""The approval API's failure modes — the ones that would void the HITL control.

Every test here is a way the gate could become theatre while still returning 200:

| Attack / mistake | What must happen |
|---|---|
| Request arrives with no validated JWT | 401 — never an anonymous approval |
| Caller is authenticated but not an approver | 403 |
| The plan changed after the human read it | 409, **before** the saga is invoked (§8.3) |
| An approval omits the digest | 400 — invariant 3 has no "approve the current plan" mode |
| A T3 plan approved by one person | 403 — holds/shred/residual need privacy *and* legal |
| A read implemented as a resume | must not happen — it would wedge the thread (V9-3) |

The last row is why `_review` and `_approve` both go through `action="describe"` and the
tests assert what the API *sent* to the saga, not merely what it returned. A test that
only checked the response body would pass for an implementation that wedges every saga it
looks at.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pii_erasure.approval import api


@pytest.fixture(autouse=True)
def _env(monkeypatch: Any) -> None:
    monkeypatch.setenv("SAGA_EXECUTOR_FUNCTION", "asdp-test-saga-executor")
    monkeypatch.delenv("HISTORY_TABLE", raising=False)


class FakeSaga:
    """Records every invocation, so a test can assert the API never delivered a resume."""

    def __init__(self, describe: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.describe = describe or {}

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        if payload.get("action") == "describe":
            return dict(self.describe)
        return {"thread_id": payload.get("thread_id"), "status": "resumed"}

    @property
    def actions(self) -> list[str]:
        return [str(call.get("action")) for call in self.calls]


MANIFEST = {
    "schemaVersion": "1.0.0",
    "manifestId": "man_1",
    "sagaId": "saga_1",
    "subjectRef": "sub_1",
    "requestId": "req_1",
    "provenance": {"discoveredAt": "2026-07-28T00:00:00Z", "agentVersion": "test@1"},
    "participants": [
        {
            "systemId": "profile-store",
            "archetype": "OPERATIONAL_NOSQL",
            "artifacts": [{"kind": "row", "locator": "profile#1", "count": 1}],
            "plannedOps": ["soft_delete", "hard_delete"],
            "order": {"phase": 2, "rank": 0},
            "deleteMethod": "PURGE",
        }
    ],
    "graceWindowDays": 30,
    "digest": "sha256:aaa",
}

PAUSED = {
    "thread_id": "saga_1",
    "status": "paused",
    "gate": "approval",
    "manifest": MANIFEST,
    "manifest_digest": "sha256:aaa",
    "tenant_id": "tenant-1",
    "subject_ref": "sub_1",
}


def _event(
    route: str,
    *,
    body: dict[str, Any] | None = None,
    groups: list[str] | None = None,
    sub: str | None = "operator-1",
    path: dict[str, str] | None = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = {}
    if sub:
        claims = {"sub": sub, "cognito:groups": groups if groups is not None else []}
    return {
        "routeKey": route,
        "pathParameters": path or {"sagaId": "saga_1"},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"jwt": {"claims": claims}} if claims else {}},
    }


def _run(event: dict[str, Any], saga: FakeSaga, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(api, "_invoke_saga", saga)
    response = api.lambda_handler(event, None)
    response["parsed"] = json.loads(response["body"])
    return response


# ─── 1. no identity, no approval ──────────────────────────────────────────────────────


def test_a_request_without_validated_claims_is_rejected(monkeypatch: Any) -> None:
    """The worst possible outcome for this API is an approval with no human behind it,
    so an absent authorizer is a hard failure and never an anonymous fallback."""
    saga = FakeSaga(PAUSED)
    response = _run(_event("POST /threads/{sagaId}/approve", sub=None), saga, monkeypatch)
    assert response["statusCode"] == 401
    assert saga.calls == [], "the saga was contacted for an unauthenticated request"


def test_an_authenticated_non_approver_cannot_approve(monkeypatch: Any) -> None:
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=["asdp-readers"],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 403
    assert "resume" not in saga.actions


def test_group_membership_comes_from_claims_not_from_the_body(monkeypatch: Any) -> None:
    """A caller asserting their own authority is the oldest trick there is."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={
            "decision": "approve",
            "manifestDigest": "sha256:aaa",
            "cognito:groups": [api.APPROVER_GROUP],
            "approver": "someone-important",
        },
        groups=[],
    )
    assert _run(event, saga, monkeypatch)["statusCode"] == 403


# ─── 2. the digest binding, enforced at the front door ────────────────────────────────


def test_a_stale_digest_is_refused_before_the_saga_is_touched(monkeypatch: Any) -> None:
    """§8.3's attack: the approver reviewed v1, the plan is now v2. The refusal must
    happen before any resume is delivered — a delivered resume is persisted against the
    interrupt and wedges the thread (V9-3)."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:STALE"},
        groups=[api.APPROVER_GROUP],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 409
    assert saga.actions == ["describe"], "a mismatched approval reached the graph"
    assert "changed after you reviewed it" in response["parsed"]["error"]


def test_an_approval_without_a_digest_is_refused(monkeypatch: Any) -> None:
    """Invariant 3 has no "approve whatever is current" mode, and adding one would
    silently reintroduce the TOCTOU hole the whole design closes."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve"},
        groups=[api.APPROVER_GROUP],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 400
    assert "resume" not in saga.actions


def test_a_matching_digest_resumes_the_saga(monkeypatch: Any) -> None:
    """The other half — the control must also let a legitimate approval through."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=[api.APPROVER_GROUP],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 200
    resume = next(call for call in saga.calls if call["action"] == "resume")["resume"]
    assert resume["decision"] == "approve"
    assert resume["digest"] == "sha256:aaa"


def test_the_recorded_approver_is_the_authenticated_one(monkeypatch: Any) -> None:
    """What lands in the ledger must be who the Gateway validated, not who the body
    claims — the ledger entry is the artifact an auditor reads years later."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={
            "decision": "approve",
            "manifestDigest": "sha256:aaa",
            "approver": "somebody-else",
        },
        groups=[api.APPROVER_GROUP],
        sub="real-operator",
    )
    _run(event, saga, monkeypatch)
    resume = next(call for call in saga.calls if call["action"] == "resume")["resume"]
    assert resume["approver"] == "real-operator"


def test_a_denial_needs_no_digest(monkeypatch: Any) -> None:
    """Denial is always safe: silence already means deny, so a denial can never be the
    thing that executes a plan the human did not read."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "deny"},
        groups=[api.APPROVER_GROUP],
    )
    assert _run(event, saga, monkeypatch)["statusCode"] == 200


def test_approving_a_saga_that_is_not_at_the_gate_is_a_conflict(monkeypatch: Any) -> None:
    """A duplicate approval arriving after the saga moved to the grace gate is exactly
    the payload that wedges a live erasure past a statutory deadline."""
    saga = FakeSaga({**PAUSED, "gate": "grace_window"})
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=[api.APPROVER_GROUP],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 409
    assert "resume" not in saga.actions


# ─── 3. two-person approval for T3 ────────────────────────────────────────────────────


def test_a_crypto_shred_plan_needs_legal_too(monkeypatch: Any) -> None:
    shred = json.loads(json.dumps(MANIFEST))
    shred["participants"][0]["deleteMethod"] = "CRYPTO_SHRED"
    shred["participants"][0]["dekRegistryRef"] = "dek/sub_1"
    saga = FakeSaga({**PAUSED, "manifest": shred})
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=[api.APPROVER_GROUP],
    )
    response = _run(event, saga, monkeypatch)
    assert response["statusCode"] == 403
    assert "T3" in response["parsed"]["error"]
    assert "resume" not in saga.actions


def test_a_t3_plan_passes_with_both_groups(monkeypatch: Any) -> None:
    shred = json.loads(json.dumps(MANIFEST))
    shred["participants"][0]["deleteMethod"] = "CRYPTO_SHRED"
    shred["participants"][0]["dekRegistryRef"] = "dek/sub_1"
    saga = FakeSaga({**PAUSED, "manifest": shred})
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=[api.APPROVER_GROUP, api.LEGAL_GROUP],
    )
    assert _run(event, saga, monkeypatch)["statusCode"] == 200


def test_a_plain_hard_delete_does_not_demand_legal(monkeypatch: Any) -> None:
    """T3 must not swallow T2, or every approval needs two people and the tier table
    stops meaning anything."""
    saga = FakeSaga(PAUSED)
    event = _event(
        "POST /threads/{sagaId}/approve",
        body={"decision": "approve", "manifestDigest": "sha256:aaa"},
        groups=[api.APPROVER_GROUP],
    )
    assert _run(event, saga, monkeypatch)["statusCode"] == 200


# ─── 4. reads never mutate ────────────────────────────────────────────────────────────


def test_reviewing_a_saga_never_delivers_a_resume(monkeypatch: Any) -> None:
    """A "read" implemented as a no-op resume would wedge every saga an operator looked
    at, and it would look like a working read until the next legitimate approval."""
    saga = FakeSaga(PAUSED)
    response = _run(_event("GET /threads/{sagaId}"), saga, monkeypatch)
    assert response["statusCode"] == 200
    assert saga.actions == ["describe"]


def test_the_review_is_the_presenter_output(monkeypatch: Any) -> None:
    saga = FakeSaga(PAUSED)
    review = _run(_event("GET /threads/{sagaId}"), saga, monkeypatch)["parsed"]["review"]
    assert review["sections"][0] == "residualRisk"
    assert review["sections"][-1] == "inventory"


def test_a_saga_with_no_manifest_says_so_rather_than_rendering_an_empty_plan(
    monkeypatch: Any,
) -> None:
    """An empty review reads as "this plan touches nothing", which is the most dangerous
    thing a review could wrongly say."""
    saga = FakeSaga({"thread_id": "saga_1", "status": "running", "gate": None})
    parsed = _run(_event("GET /threads/{sagaId}"), saga, monkeypatch)["parsed"]
    assert parsed["review"] is None
    assert "not at the approval gate" in parsed["note"]


def test_an_unknown_route_is_a_404(monkeypatch: Any) -> None:
    saga = FakeSaga(PAUSED)
    response = _run(_event("DELETE /threads/{sagaId}"), saga, monkeypatch)
    assert response["statusCode"] == 404
    assert saga.calls == []


# ─── 5. invariant 5 on the way out ────────────────────────────────────────────────────


def test_a_leaked_email_is_scrubbed_from_every_response(monkeypatch: Any) -> None:
    leaky = json.loads(json.dumps(MANIFEST))
    leaky["participants"][0]["artifacts"][0]["locator"] = "grace@example.invalid"
    saga = FakeSaga({**PAUSED, "manifest": leaky})
    response = _run(_event("GET /threads/{sagaId}"), saga, monkeypatch)
    assert "grace@example.invalid" not in response["body"]
