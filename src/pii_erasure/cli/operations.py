"""The operator commands' deployed-stack logic, kept out of the Typer layer.

`cli/main.py` parses; this module talks to AWS. The split matters because the walkthrough
is M8's deployed gate, and a gate that lives inside argument-parsing code is one nobody
can call from a test.

**Approval goes through the HTTP API, never around it.** `approve` obtains a Cognito
token and calls the authenticated route. It would be far easier to invoke the approval
Lambda directly, or the saga executor directly, and it would make the walkthrough prove
nothing: the property M8 exists to demonstrate is that an irreversible act required an
authenticated human. A CLI that quietly bypassed the front door would produce a green
walkthrough over a control that was never exercised. So missing operator credentials are
a **loud failure with the commands to fix them**, not a fallback path.

**The pause is shown as absence of compute** (ADR-016). `threads` reports what each saga
is waiting for and states plainly that nothing is running — the checkpoint row *is* the
pause, and the walkthrough's job is to make that visible rather than to assert it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from pii_erasure.approval.presenter import baseline_from_history, present, render_text
from pii_erasure.ledger.chain import LedgerEntry
from pii_erasure.ledger.verify import verify_chain
from pii_erasure.ledger.writer import PARTITION_KEY as LEDGER_PARTITION_KEY
from pii_erasure.manifest.models import Manifest

#: How long the walkthrough waits for a phase to settle before calling it stuck. Dev
#: stacks compress the grace window to minutes via stack parameters — never by skipping
#: the scheduler, which would test a different system than the one that ships.
POLL_TIMEOUT_SECONDS = 900
POLL_INTERVAL_SECONDS = 10

#: How long a thread may have no checkpoint at all before the wait gives up. An async
#: intake takes a second or two to land; two minutes of nothing means it never did.
NO_CHECKPOINT_GRACE_SECONDS = 120


#: The operator pool synthesises as `UsernameAttributes: ["email"]`, so a username must
#: **parse** as an address — Cognito validates the shape and never the mailbox. With
#: `--message-action SUPPRESS` and a `--permanent` password no mail is ever attempted, so
#: a real inbox is not needed and asking for one would invite a real address into a demo
#: system. `.invalid` is reserved by RFC 6761 and cannot resolve, so this example can
#: never reach anybody even if a later change stops suppressing.
#:
#: The audit trail is unaffected by the choice: `approval/api.py` records the Cognito
#: `sub` — a UUID — as the approver, never the address.
EXAMPLE_OPERATOR = "operator@example.invalid"


class OperationError(RuntimeError):
    """A deployed-stack operation failed in a way the operator must see."""


def stage() -> str:
    return os.environ.get("PII_ERASURE_STAGE", "dev")


def outputs(*stacks: str) -> dict[str, str]:
    """CloudFormation outputs, merged. Read from the stack, never from the environment.

    An operator whose `.env` points at a torn-down stage would otherwise get confident
    errors from the wrong account rather than an obvious one from this call.
    """
    import boto3

    cfn = boto3.client("cloudformation")
    found: dict[str, str] = {}
    for name in stacks:
        described = cfn.describe_stacks(StackName=f"asdp-{stage()}-{name}")["Stacks"][0]
        for output in described.get("Outputs", []):
            found[output["OutputKey"]] = output["OutputValue"]
    return found


# ─── the saga, read and resumed ───────────────────────────────────────────────────────


def invoke_saga(payload: dict[str, Any]) -> dict[str, Any]:
    import boto3

    response = boto3.client("lambda").invoke(
        FunctionName=f"asdp-{stage()}-saga-executor",
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        raise OperationError(f"saga executor failed: {body}")
    return dict(body)


def list_threads(limit: int = 50) -> dict[str, Any]:
    return invoke_saga({"action": "threads", "limit": limit})


def describe_thread(thread_id: str) -> dict[str, Any]:
    return invoke_saga({"action": "describe", "thread_id": thread_id})


def resume_thread(thread_id: str) -> dict[str, Any]:
    """Deliver the wake the thread's current gate is actually waiting for.

    The gate is read first rather than guessed. A resume shaped for a different gate is
    persisted against the pending interrupt before the node sees it, so a wrong guess
    does not fail once — it wedges the thread permanently (V9-3). The executor refuses
    such a payload, and this reads the gate so it never sends one.
    """
    state = describe_thread(thread_id)
    gate = str(state.get("gate") or "")
    if not gate:
        raise OperationError(f"{thread_id} is not paused (status={state.get('status')!r})")
    if gate == "approval":
        raise OperationError(
            f"{thread_id} is waiting for a human at the approval gate. Use `erasure approve "
            f"--thread {thread_id} --decision approve|deny` — a manual resume would bypass "
            f"the digest binding that makes the approval mean anything (invariant 3)."
        )
    return invoke_saga(
        {"action": "resume", "thread_id": thread_id, "resume": {"wake_reason": gate}}
    )


# ─── discovery ────────────────────────────────────────────────────────────────────────


def run_discovery(subject_ref: str, *, tenant: str = "default") -> dict[str, Any]:
    """One discovery pass on the deployed Runtime — the same call `make eval` makes."""
    import boto3

    runtime_arn = outputs("runtime")["RuntimeArn"]
    session = f"cli-{subject_ref}-{'0' * 40}"[:64]
    response = boto3.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"subjectRef": subject_ref, "tenant": tenant}).encode(),
    )
    parsed: dict[str, Any] = json.loads(response["response"].read())
    return parsed


# ─── the authenticated approval path ──────────────────────────────────────────────────


def operator_token() -> str:
    """A Cognito access token for the operator identity, or a loud, actionable failure.

    Deliberately has no fallback. Invoking the approval Lambda directly would work, and
    would make every walkthrough green over a control nobody exercised.
    """
    import boto3

    username = os.environ.get("PII_ERASURE_OPERATOR_USER", "")
    password = os.environ.get("PII_ERASURE_OPERATOR_PASSWORD", "")
    api = outputs("api")
    if not username or not password:
        raise OperationError(
            "no operator credentials. Approval must go through the authenticated API, so "
            "there is no bypass here on purpose. Create one:\n\n"
            f"  aws cognito-idp admin-create-user --user-pool-id {api['OperatorPoolId']} \\\n"
            f"      --username {EXAMPLE_OPERATOR} --message-action SUPPRESS \\\n"
            f"      --user-attributes Name=email,Value={EXAMPLE_OPERATOR} "
            f"Name=email_verified,Value=true\n"
            f"  aws cognito-idp admin-set-user-password --user-pool-id {api['OperatorPoolId']} \\\n"
            f"      --username {EXAMPLE_OPERATOR} --password '<12+ chars>' --permanent\n"
            f"  aws cognito-idp admin-add-user-to-group --user-pool-id {api['OperatorPoolId']} \\\n"
            f"      --username {EXAMPLE_OPERATOR} --group-name asdp-approvers\n\n"
            "then set PII_ERASURE_OPERATOR_USER and PII_ERASURE_OPERATOR_PASSWORD.\n"
            "A T3 plan (holds, crypto-shred, or residual risk) also needs asdp-legal."
        )
    result = boto3.client("cognito-idp").initiate_auth(
        ClientId=api["OperatorClientId"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    challenge = result.get("ChallengeName")
    if challenge:
        raise OperationError(
            f"Cognito returned challenge {challenge!r} — complete it once in the console, "
            "or set a permanent password with admin-set-user-password."
        )
    token: str = result["AuthenticationResult"]["AccessToken"]
    return token


def api_call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = outputs("api")["OperatorApiUrl"].rstrip("/") + path
    # The URL comes from a CloudFormation output, not from user input — but "it came from
    # a trusted place" is what every SSRF post-mortem says, and an API Gateway output is
    # only trusted while the stage name is. Pinned to https so a mangled output cannot
    # turn this into a file:// read of the operator's disk.
    if not url.startswith("https://"):
        raise OperationError(f"refusing a non-https operator API endpoint: {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme pinned above
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "authorization": operator_token(),
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:  # noqa: S310
            parsed: dict[str, Any] = json.loads(response.read() or b"{}")
            return parsed
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise OperationError(f"{method} {path} -> HTTP {error.code}: {detail}") from error


def submit_decision(thread_id: str, decision: str) -> dict[str, Any]:
    """Approve or deny through the API, echoing the digest that was reviewed.

    The digest is read from the *saga's* current state and sent back, which is what makes
    a stale screen fail: if the plan moved on between the review and this call, the API
    compares what is echoed against what is pending and refuses (§8.3).
    """
    review = api_call("GET", f"/threads/{thread_id}")
    body: dict[str, Any] = {"decision": decision}
    if decision == "approve":
        digest = ((review.get("review") or {}).get("manifestDigest")) or ""
        if not digest:
            raise OperationError(
                f"{thread_id} has no signed manifest to approve "
                f"(gate={review.get('gate')!r}, status={review.get('status')!r})"
            )
        body["manifestDigest"] = digest
    return api_call("POST", f"/threads/{thread_id}/approve", body)


def review_text(thread_id: str) -> str:
    """The approval view as text — the same sections, in the same order, as the API's."""
    state = describe_thread(thread_id)
    manifest_body = state.get("manifest")
    if not manifest_body:
        return f"{thread_id}: no signed manifest yet (gate={state.get('gate')!r})"
    manifest = Manifest.model_validate(manifest_body)
    return render_text(present(manifest, baseline=baseline_from_history([])))


# ─── the ledger ───────────────────────────────────────────────────────────────────────


def ledger_entries(saga_id: str | None = None) -> list[LedgerEntry]:
    """Entries for one saga, or every saga the table holds.

    Without a saga id this scans, which is fine for an operator command against a dev
    stack and would not be fine as a service call. It is here because an auditor's first
    question is "show me everything", and answering it with "per saga only" invites a
    second, un-chained export nobody verifies.
    """
    import boto3

    from pii_erasure.ledger.writer import LedgerWriter

    table = outputs("foundation")["LedgerTable"]
    writer = LedgerWriter(table, client=boto3.client("dynamodb"))
    if saga_id:
        return writer.entries(saga_id)

    # `saga_id`, not `sagaId`: the DynamoDB attribute is the table's partition key and is
    # snake_case (`ledger/writer.py::_to_item`). `sagaId` is a camelCase key inside the
    # digested *body*, and projecting it returned an empty item for every row — so this
    # command raised `KeyError: 'sagaId'` on its first result and had never once run
    # (V12-4). Every test of `verify_ledger` monkeypatched this function away.
    paginator = boto3.client("dynamodb").get_paginator("scan")
    saga_ids: set[str] = set()
    for page in paginator.paginate(TableName=table, ProjectionExpression=LEDGER_PARTITION_KEY):
        for item in page.get("Items", []):
            saga_ids.add(item[LEDGER_PARTITION_KEY]["S"])
    entries: list[LedgerEntry] = []
    for found in sorted(saga_ids):
        entries.extend(writer.entries(found))
    return entries


def verify_ledger(saga_id: str | None = None) -> tuple[int, list[LedgerEntry]]:
    """Verify the hash chain per saga. Returns (verified count, all entries).

    Chains are per saga, so verification is per saga: running `verify_chain` over the
    concatenation of several sagas' entries would fail on the boundary between them and
    report tampering that did not happen — a false alarm in an audit tool is how the tool
    stops being read.
    """
    entries = ledger_entries(saga_id)
    by_saga: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        by_saga.setdefault(entry.saga_id, []).append(entry)
    verified = 0
    for chain in by_saga.values():
        verified += verify_chain(sorted(chain, key=lambda e: e.seq))
    return verified, entries


# ─── the walkthrough ──────────────────────────────────────────────────────────────────


#: Statuses from which a saga will never reach a later gate on its own. Waiting on one is
#: waiting on a corpse, and the fifteen minutes spent doing so is fifteen minutes during
#: which the operator believes work is in progress. `state.py` owns these names; they are
#: repeated rather than imported because `cli/` must not depend on the saga package's
#: framework-bearing modules.
TERMINAL_STATUSES = frozenset(
    # `no_data` was added to the saga at V11-4 and NOT added here — caught by
    # `test_the_terminal_set_and_the_explanations_agree`, which is the whole reason
    # two hand-maintained lists get a test that compares them. A saga ending at
    # `no_data` would have been polled for the full fifteen minutes.
    {"completed", "compensated", "blocked", "stuck", "aborted", "no_data"}
)


#: What each terminal status MEANS, for an operator who did not write the saga. "halted
#: at status 'blocked'. Errors: none recorded" is accurate and tells you nothing — worse,
#: "none recorded" reads as a swallowed error when in fact a blocked saga is the system
#: refusing correctly and having nothing to apologise for (V11-8).
_TERMINAL_EXPLANATIONS = {
    "blocked": (
        "A legal hold vetoed this erasure before anything mutated. That is the hold "
        "working, not a failure — check the ledger for BLOCKED_BY_HOLD and the holdIds."
    ),
    "no_data": (
        "Discovery found the subject in no system at all. Legitimate, and still owed an "
        "answer under Art. 12(3) — but nothing was erased because there was nothing to erase."
    ),
    "aborted": (
        "The manifest digest did not match after approval, so phase 3 never ran. Nothing "
        "irreversible happened; a new manifest needs a new approval (invariant 3)."
    ),
    "stuck": (
        "Phase 3 ran out of forward road and raised the DLQ. It does NOT compensate "
        "(invariant 6) — a human remediates and resumes. Check the SQS DLQ."
    ),
    "compensated": (
        "Phase 2 failed and the soft deletes were unwound. The subject's data is intact."
    ),
}


def _why(status: str, state: dict[str, Any]) -> str:
    """One sentence of meaning, then the recorded errors if there are any."""
    explanation = _TERMINAL_EXPLANATIONS.get(status, "")
    errors = state.get("errors")
    detail = f" Errors: {errors}" if errors else ""
    return f"{explanation}{detail}" or "No explanation recorded."


def wait_for(
    thread_id: str,
    *,
    gate: str | None = None,
    status: str | None = None,
    notify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Poll the checkpoint until the saga reaches a gate or a terminal status.

    Polling the *checkpoint* rather than holding an invocation is the point: between
    calls, nothing of ours is running. That is the property ADR-016 is built on, and the
    walkthrough demonstrates it by being able to stop and restart at any moment.

    A saga that halts somewhere else — `stuck`, `aborted`, `blocked` — ends the wait
    immediately and says so. The alternative is polling a finished saga until the timeout
    and then reporting "did not reach the gate", which is true, useless, and fifteen
    minutes late.
    """
    started = time.time()
    deadline = started + POLL_TIMEOUT_SECONDS
    last: dict[str, Any] = {}
    seen = ""
    while time.time() < deadline:
        last = describe_thread(thread_id)
        current = str(last.get("status") or "")
        # Say something. Discovery alone runs ~40 seconds and phase 2 visits eight real
        # services, so a silent poll looks identical to a hang — which is exactly how
        # V11-4 was first reported ("why does the terminal get stuck?"). Progress is
        # printed on change and at least once a minute.
        if notify is not None:
            elapsed = int(time.time() - started)
            marker = f"{current}/{last.get('gate')}"
            if marker != seen or elapsed % 60 < POLL_INTERVAL_SECONDS:
                notify(f"   [{elapsed:>4}s] status={current or '?'} gate={last.get('gate')}")
                seen = marker
        if gate and last.get("gate") == gate:
            return last
        if status and current == status:
            return last
        if current in TERMINAL_STATUSES and current != status:
            raise OperationError(
                f"{thread_id} halted at status {current!r} while waiting for "
                f"{gate or status!r}. {_why(current, last)}"
            )
        # `describe` returns "unknown" for a thread with no checkpoint. Right after an
        # async intake that just means the executor has not started yet; if it persists,
        # the start invocation never landed — and a silent async failure looks exactly
        # like a slow one, so it must not be waited out in silence (V11-3).
        if current == "unknown" and time.time() > started + NO_CHECKPOINT_GRACE_SECONDS:
            raise OperationError(
                f"{thread_id} has no checkpoint after {NO_CHECKPOINT_GRACE_SECONDS}s — "
                f"the start invocation did not land. Check the saga-executor log group."
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise OperationError(
        f"{thread_id} did not reach {gate or status!r} within {POLL_TIMEOUT_SECONDS}s "
        f"(last: gate={last.get('gate')!r} status={last.get('status')!r})"
    )
