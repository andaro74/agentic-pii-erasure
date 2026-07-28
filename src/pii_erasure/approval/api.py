"""The operator HTTP API — intake, approval, and reads (ARCHITECTURE §8.2).

A Lambda behind an API Gateway HTTP API whose every route carries a Cognito JWT
authorizer. It holds **no graph and no model**: it validates, then invokes the
saga-executor Lambda. That is why this module imports no framework despite living next
to `gate.py` on invariant 0's allowlist — the approval *service* is a different identity
from the saga (`asdp-approval-service`, ADR-018), and an HTTP front door that could load
the graph would be a second place saga state is touched.

**The digest check happens here, and again in the node.** `POST /threads/{id}/approve`
compares the digest the approver echoes against the digest of the plan the saga is
actually paused on, and refuses on mismatch — before a token is minted, before the saga
is invoked at all. `nodes/approval_gate.py` checks the same thing again when the resume
lands. That is not redundancy: a caller with the executor's permissions can skip this
API entirely, so the node's check is the control and this one is the *early* failure that
tells an operator their screen was stale rather than letting them think they approved.

**Silence is denial, and this API cannot express "approve later".** There is no route
that extends an approval window; the scheduler's timeout is the only thing that resolves
an unanswered gate, and it resolves it as a denial (§8.2).

**The authenticated identity is recorded, never asserted by the caller.** `approver`
comes from the JWT claims the Gateway validated, so a request body claiming to be
somebody else changes nothing. Two-person approval (T3) is enforced by comparing the
*authenticated* subject against the one who approved first.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3

from pii_erasure.approval.presenter import baseline_from_history, present, required_tier
from pii_erasure.manifest.models import Manifest
from pii_erasure.observability.logging import configure_logging, get_logger
from pii_erasure.observability.redact import scrub_mapping

configure_logging()
_log = get_logger(__name__)

#: Cognito group that may approve. Membership is checked on the *authorizer's* claims,
#: not on anything the caller sends.
APPROVER_GROUP = "asdp-approvers"
#: T3 needs a second, different human — privacy plus legal (§8.1).
LEGAL_GROUP = "asdp-legal"


class ApiError(Exception):
    """An HTTP-shaped failure. Carries the status so routes can raise and stop."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _lambda() -> Any:
    return boto3.client("lambda")


def _invoke_saga(payload: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
    """Call the executor. `wait=False` fires and forgets.

    **Which actions may be waited on is a property of the saga, not a preference.** The
    executor drives the graph to its next interrupt, and for `start` that means discovery,
    planning, and every phase-2 soft delete — minutes of work. An HTTP request cannot hold
    that: API Gateway's integration ceiling is 30 seconds, so a synchronous intake times
    out at 29s and the caller sees a 503 with no saga id, for a saga that is in fact
    running perfectly well (V11-3).

    Reads (`describe`, `threads`) and the approval resume are bounded and stay
    synchronous. The approval resume mints a token, schedules the grace wake, and returns
    at the next interrupt; the hard deletes happen when the *scheduler* fires, not here.
    """
    response = _lambda().invoke(
        FunctionName=os.environ["SAGA_EXECUTOR_FUNCTION"],
        InvocationType="RequestResponse" if wait else "Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    if not wait:
        # 202 from Lambda means "queued", and that is all there is to report. Failures
        # after this point surface in the saga's own status, which is what the operator
        # polls — there is no synchronous answer to wait for and pretending otherwise is
        # what caused V11-3.
        return {"status": "accepted"}
    body = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        # The executor's stack trace can name state; the operator gets the fact, and
        # CloudWatch keeps the detail under the saga's own correlation key.
        _log.error("saga_invoke_failed", thread_id=payload.get("thread_id"))
        raise ApiError(502, "the saga executor rejected this request")
    return dict(body)


def _claims(event: dict[str, Any]) -> dict[str, Any]:
    """The JWT claims API Gateway validated. Absent means the authorizer did not run.

    Treated as a hard failure rather than an anonymous fallback: a route that reaches
    this handler unauthenticated is a misconfigured stack, and serving it would be the
    single worst outcome this API has — an approval with no human behind it.
    """
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (ctx.get("jwt") or {}).get("claims")
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise ApiError(401, "no validated identity on this request")
    return claims


def _groups(claims: dict[str, Any]) -> set[str]:
    """The caller's Cognito groups, from whichever shape the authorizer produced.

    **API Gateway's JWT authorizer flattens array claims to a string**, and it does so
    Java-style: `["a", "b"]` arrives as the literal `[a b]` — brackets, **space**
    separated. A direct `initiate_auth` response instead gives a real list, and some
    setups produce a comma-separated string.

    The first version split on commas only. One group worked by accident (`[asdp-approvers]`
    strips to a single token); **two groups produced one nonsense element**
    `"asdp-approvers asdp-legal"` that matched nothing, so an operator in both required
    groups was refused for having neither (V11-5). Splitting on both separators is not
    defensive coding for its own sake — it is three real encodings of one claim.
    """
    raw = claims.get("cognito:groups") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw.strip().strip("[]"))
    return {str(item).strip() for item in raw if str(item).strip()}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    route = str(event.get("routeKey", ""))
    try:
        claims = _claims(event)
        body = _body(event)
        params = event.get("pathParameters") or {}

        if route == "POST /requests":
            # 202: the work is queued, not done. A 200 here would tell an operator the
            # erasure had completed when discovery has not even started.
            return _ok(_intake(body, claims), status=202)
        if route == "GET /threads":
            return _ok(_invoke_saga({"action": "threads"}))
        if route == "GET /threads/{sagaId}":
            return _ok(_review(str(params["sagaId"])))
        if route == "POST /threads/{sagaId}/approve":
            return _ok(_approve(str(params["sagaId"]), body, claims))
        raise ApiError(404, f"no route for {route!r}")
    except ApiError as error:
        return _fail(error.status, error.message)
    except KeyError as error:
        return _fail(400, f"missing field {error}")


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ApiError(400, "body is not valid JSON") from error
    return parsed if isinstance(parsed, dict) else {}


def _intake(body: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """Accept an erasure request. T0 — no gate; discovery is read-only.

    **Accepted, not completed.** The saga is started asynchronously and this returns 202
    with the id to poll. That is not an optimisation: discovery plus phase 2 takes minutes
    and API Gateway allows 30 seconds, so a synchronous intake reports failure for work
    that succeeded (V11-3). It also matches how the rest of the system already behaves —
    the saga's progress is read from the checkpoint, never from a held connection.
    """
    required = ("sagaId", "subjectRef", "requestId", "tenantId")
    missing = [field for field in required if not body.get(field)]
    if missing:
        raise ApiError(400, f"missing {missing}")
    _log.info("intake", thread_id=body["sagaId"], requested_by=claims["sub"])
    _invoke_saga(
        {
            "action": "start",
            "saga": {
                "saga_id": body["sagaId"],
                "subject_ref": body["subjectRef"],
                "request_id": body["requestId"],
                "tenant_id": body["tenantId"],
                "manifest": body.get("manifest"),
            },
        },
        wait=False,
    )
    return {
        "sagaId": body["sagaId"],
        "status": "accepted",
        "poll": f"/threads/{body['sagaId']}",
    }


def _review(saga_id: str) -> dict[str, Any]:
    """The approval view for one paused saga — the presenter's output, over HTTP."""
    state = _invoke_saga({"action": "describe", "thread_id": saga_id})
    manifest_body = state.get("manifest")
    if not manifest_body:
        # A saga with no manifest is not yet at the approval gate. Saying so beats
        # rendering an empty review that looks like a plan touching nothing.
        return {
            "sagaId": saga_id,
            "status": state.get("status"),
            "gate": state.get("gate"),
            "review": None,
            "note": "no signed manifest yet — this saga is not at the approval gate",
        }
    manifest = Manifest.model_validate(manifest_body)
    history = _tenant_history(str(state.get("tenant_id") or ""))
    return {
        "sagaId": saga_id,
        "status": state.get("status"),
        "gate": state.get("gate"),
        "review": present(manifest, baseline=baseline_from_history(history)),
    }


def _tenant_history(tenant_id: str) -> list[dict[str, Any]]:
    """Prior deletions for this tenant, as the presenter's baseline input.

    Returns `[]` when there is no history table configured or the read fails, and that
    is safe **because** the presenter treats an empty baseline as `baseline-unavailable`
    rather than as "nothing unusual". If it treated it as a clean comparison, this
    except clause would be a silent downgrade of a control.
    """
    table_name = os.environ.get("HISTORY_TABLE", "")
    if not table_name or not tenant_id:
        return []
    try:
        response = boto3.client("dynamodb").query(
            TableName=table_name,
            KeyConditionExpression="tenantId = :t",
            ExpressionAttributeValues={":t": {"S": tenant_id}},
            Limit=200,
        )
    except Exception:
        _log.warning("history_unavailable", tenant_id=tenant_id)
        return []
    return [
        {"systems": list(item.get("systems", {}).get("SS") or [])}
        for item in response.get("Items", [])
    ]


def _approve(saga_id: str, body: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """Approve or deny. The digest is checked before anything is minted (invariant 3)."""
    decision = str(body.get("decision", ""))
    if decision not in {"approve", "deny"}:
        raise ApiError(400, "decision must be 'approve' or 'deny'")
    held = _groups(claims)
    if APPROVER_GROUP not in held:
        # Naming what was seen turns "you are not in the group" into a diagnosis. The
        # first version said only what was required, so a claim this API had *mis-parsed*
        # was indistinguishable from a group the operator had genuinely not been added
        # to — and the wrong one of those two sends you to the Cognito console (V11-5).
        raise ApiError(
            403,
            f"approval requires membership of {APPROVER_GROUP!r}; this token carries "
            f"{sorted(held) or 'no groups'}",
        )

    state = _invoke_saga({"action": "describe", "thread_id": saga_id})
    if state.get("gate") != "approval":
        raise ApiError(409, f"saga is not at the approval gate (gate={state.get('gate')!r})")

    pending_digest = str(state.get("manifest_digest") or "")
    if decision == "approve":
        echoed = str(body.get("manifestDigest", ""))
        if not echoed:
            raise ApiError(400, "an approval must echo the digest that was reviewed")
        if echoed != pending_digest:
            # §8.3's TOCTOU attack, caught at the front door. The plan changed between
            # render and submit, so what the human read is not what would execute.
            _log.warning("approval_digest_mismatch", thread_id=saga_id)
            raise ApiError(
                409,
                "the plan changed after you reviewed it — reload the review and approve "
                "the new manifest, which requires a fresh decision",
            )
        manifest_body = state.get("manifest") or {}
        tier = required_tier(Manifest.model_validate(manifest_body)) if manifest_body else "T2"
        if tier == "T3" and LEGAL_GROUP not in held:
            raise ApiError(
                403,
                "this plan is tier T3 — holds, crypto-shred, or disclosed residual risk — "
                f"and needs a second approver from {LEGAL_GROUP!r}",
            )

    _log.info("approval_decision", thread_id=saga_id, decision=decision, approver=claims["sub"])
    return _invoke_saga(
        {
            "action": "resume",
            "thread_id": saga_id,
            "resume": {
                "decision": decision,
                "digest": pending_digest if decision == "approve" else None,
                "approver": str(claims["sub"]),
            },
        }
    )


def _ok(body: dict[str, Any], *, status: int = 200) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(scrub_mapping(body)),
    }


def _fail(status: int, message: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"error": message}),
    }
