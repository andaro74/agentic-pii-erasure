"""M5's deployed gate: the full three-phase saga against the real stack.

**This costs money and takes ~15-25 minutes.** Athena round-trips and the first
Aurora touch (a ~2-minute resume from 0 ACU) dominate; the saga's own verify pass
runs three times per completed arc (T+0 and the two dev-compressed sweeps).

Scenarios, and how each maps to the roadmap's "done when":

* **happy path with pause/approve/resume** — start pauses at the approval gate with
  the manifest digest; approve runs phase 3 to the sweep gate; two sweep wakes (via
  the real resume Lambda) complete the saga. Corroborated from outside the saga:
  tombstone row present, ledger hash chain verifies, Cognito/DynamoDB actually empty.
* **kill mid-phase / phase-3 failure** — one scenario, one real lever: an IN_FLIGHT
  claim planted in the idempotency table makes `billing-ledger.hard_delete` raise
  `ReplayInFlightError` (a genuine operational state, not a mock). Phase 3 stops at
  billing with five receipts already checkpointed, the DLQ carries the failure, and
  NOTHING is restored. Remediation (deleting the stale claim) plus an operator
  resume finishes the arc — each system's HARD_DELETE_APPLIED appearing exactly
  once in the ledger is the "zero duplicate participant calls" assertion.
* **phase-2 failure → full compensation** — a ghost participant the stack does not
  have fails its soft delete; everything soft-deleted before it is restored, in
  reverse order, and the Cognito user is enabled again.
* **post-approval manifest mutation → abort** — the resume carries a digest for a
  plan the approver never saw; the saga unwinds to re-approval with zero hard
  deletes. (The deeper in-band defence — the token-digest check at the hard_delete
  node — is exercised hermetically at the node seam in unit tests; the Gateway-side
  Cedar enforcement of the same binding is M6's deployed gate.)

Seeding and teardown reuse the conformance rig's writers and cleanup via importlib —
the same proven code path, not a second implementation.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import time
import uuid
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from evals.fixtures.generator import FixtureGenerator
from pii_erasure.contract import Archetype, Artifact, Verb
from pii_erasure.contract.idempotency import idempotency_key
from pii_erasure.ledger import LedgerWriter, verify_chain
from pii_erasure.manifest import ManifestParticipant, OrderSlot
from pii_erasure.saga.tombstone import subject_hash
from tests.conftest import build_fixture_manifest

pytestmark = pytest.mark.integration

STAGE = os.environ.get("PII_ERASURE_STAGE", "dev")
REPO = Path(__file__).resolve().parents[2]

_EXECUTOR = f"asdp-{STAGE}-saga-executor"
_RESUME = f"asdp-{STAGE}-saga-resume"


def _load_conformance() -> Any:
    spec = importlib.util.spec_from_file_location(
        "conformance_rig", REPO / "tests" / "conformance" / "test_contract.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def rig() -> Any:
    module = _load_conformance()
    from pii_erasure.cli.main import _seed_clients, _stack_config

    config = _stack_config(os.environ.get("PII_ERASURE_TENANT", "meridian"))
    # allow_ses_sandbox=True: in a sandbox account the contact still gets seeded and
    # the missing suppression entry is recorded as a degraded capability — which is
    # exactly what the saga will then observe. Deterministic in both account states.
    generator = FixtureGenerator(clients=_seed_clients(), config=config, allow_ses_sandbox=True)
    return module, generator, config


@pytest.fixture(scope="session")
def lambda_client() -> Any:
    # One invocation may legitimately run for minutes (Aurora resume, Athena).
    # retries=0 is load-bearing: a client-side retry of `invoke` would be a
    # duplicate saga step delivered by our own test harness.
    client = boto3.client(
        "lambda",
        config=Config(read_timeout=910, connect_timeout=10, retries={"max_attempts": 0}),
    )
    try:
        client.get_function(FunctionName=_EXECUTOR)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        pytest.skip("saga stack is not deployed yet — run `make deploy-dev` (docs/ROADMAP.md M5)")
    return client


def _invoke(client: Any, function: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.invoke(FunctionName=function, Payload=json.dumps(payload).encode())
    body = json.loads(response["Payload"].read())
    assert "FunctionError" not in response, f"{function} failed: {body}"
    return dict(body)


def _seed(rig: Any, handle: str, system_ids: tuple[str, ...]) -> None:
    module, generator, _config = rig
    throwaway = {
        "subjectRef": handle,
        "displayName": "Saga Integration Fixture",
        "email": f"{handle}@meridian.invalid",
    }
    writers = generator._writers()
    for system_id in system_ids:
        writers[system_id](throwaway, module.PLACEMENTS[system_id])

    # Surface a degraded environment rather than absorbing it. In an SES-sandbox
    # account the suppression entry cannot be seeded, so notify-suppression returns
    # APPLIED instead of PARTIAL and the RESIDUAL_BY_DESIGN archetype is not
    # exercised. The saga arc itself is unaffected — clean is always an acceptable
    # verify — but a reader of a green run deserves to know which claim went untested.
    for gap in generator._degraded:
        warnings.warn(f"degraded environment: {gap}", stacklevel=2)


def _teardown(rig: Any, handle: str, system_ids: tuple[str, ...]) -> None:
    module, generator, config = rig
    for system_id in system_ids:
        module._cleanup(system_id, handle, generator, config)


def _ledger_events(saga_id: str) -> list[Any]:
    return LedgerWriter(f"asdp-{STAGE}-ledger").entries(saga_id)


def _approve(client: Any, thread_id: str, digest: str) -> dict[str, Any]:
    return _invoke(
        client,
        _EXECUTOR,
        {
            "action": "resume",
            "thread_id": thread_id,
            "resume": {
                "decision": "approve",
                "digest": digest,
                "approver": "integration-suite",
            },
        },
    )


def _run_sweeps_to_completion(client: Any, thread_id: str) -> dict[str, Any]:
    result = _invoke(client, _RESUME, {"thread_id": thread_id, "wake_reason": "sweep_t7"})
    assert result["status"] == "paused", f"after sweep_t7: {result}"
    result = _invoke(client, _RESUME, {"thread_id": thread_id, "wake_reason": "sweep_t30"})
    return result


@contextlib.contextmanager
def _seeded_subject(rig: Any, system_ids: tuple[str, ...]) -> Iterator[str]:
    """A throwaway subject that is torn down however this block exits — including
    when SEEDING ITSELF raises partway (V9-4).

    Seeding walks the systems in order, so a failure on the seventh leaves six
    populated. When that happened during fixture *setup*, pytest never reached the
    teardown and the residue outlived the run — V8-13's problem returning through a
    door left open. `_teardown` tolerates absence per system, so unwinding a partial
    seed is the same call as unwinding a complete one.
    """
    handle = f"sub_saga_{uuid.uuid4().hex[:12]}"
    try:
        _seed(rig, handle, system_ids)
        yield handle
    finally:
        _teardown(rig, handle, system_ids)


@pytest.fixture
def subject_all_eight(rig: Any) -> Iterator[str]:
    module, _generator, _config = rig
    with _seeded_subject(rig, tuple(module.PLACEMENTS)) as handle:
        yield handle


# ─── happy path ───────────────────────────────────────────────────────────────────────


def test_happy_path_pause_approve_resume_to_certified_clean(
    rig: Any, lambda_client: Any, subject_all_eight: str
) -> None:
    _module, generator, config = rig
    handle = subject_all_eight
    saga_id = f"saga_{uuid.uuid4().hex[:12]}"
    manifest = build_fixture_manifest(saga_id=saga_id, subject_ref=handle)

    started = _invoke(
        lambda_client,
        _EXECUTOR,
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
    assert started["status"] == "paused", started
    assert started["gate"] == "approval"
    digest = started["interrupt"]["manifestDigest"]
    assert digest
    assert digest.startswith("sha256:")
    # Residual risk is disclosed to the approver BEFORE approval — invariant 7
    # starts at plan time, and the payload leads with it.
    assert "residualRisk" in started["interrupt"]

    approved = _approve(lambda_client, saga_id, digest)
    assert approved["status"] == "paused", approved
    assert approved["gate"] == "sweep", approved

    # A duplicate approval must not re-run anything AND must not wedge the saga: the
    # thread is paused at the sweep gate, which never asked an approval question, so
    # the executor refuses the payload without letting it reach the graph (V9-3).
    stray = _invoke(
        lambda_client,
        _EXECUTOR,
        {
            "action": "resume",
            "thread_id": saga_id,
            "resume": {"decision": "approve", "digest": digest, "approver": "dup"},
        },
    )
    assert stray["status"] == "resume_rejected", stray
    assert stray["gate"] == "sweep", stray

    final = _run_sweeps_to_completion(lambda_client, saga_id)
    assert final["status"] == "completed", final

    # ── corroborate from OUTSIDE the saga ────────────────────────────────────
    entries = _ledger_events(saga_id)
    assert verify_chain(entries) == len(entries)
    assert entries
    applied = [e.body["systemId"] for e in entries if e.event_type == "HARD_DELETE_APPLIED"]
    assert sorted(applied) == sorted(set(applied))
    assert len(applied) == 8

    tombstone = boto3.client("dynamodb").get_item(
        TableName=f"asdp-{STAGE}-tombstones",
        Key={"subject_hash": {"S": subject_hash(handle)}},
        ConsistentRead=True,
    )
    assert "Item" in tombstone, "the tombstone registry must outlive the subject"

    with pytest.raises(ClientError) as excinfo:
        generator._clients["cognito-idp"].admin_get_user(
            UserPoolId=config["userPoolId"], Username=handle
        )
    assert excinfo.value.response["Error"]["Code"] == "UserNotFoundException"
    remaining = (
        generator._clients["dynamodb"]
        .Table(config["profileTable"])
        .query(
            KeyConditionExpression="subject_ref = :s",
            ExpressionAttributeValues={":s": handle},
            ConsistentRead=True,
        )
        .get("Items", [])
    )
    assert remaining == []

    # A re-request for the erased subject is refused at intake by the tombstone.
    rerun = _invoke(
        lambda_client,
        _EXECUTOR,
        {
            "action": "start",
            "saga": {
                "saga_id": f"saga_{uuid.uuid4().hex[:12]}",
                "subject_ref": handle,
                "request_id": "dsr_rerun",
                "tenant_id": "meridian",
                "manifest": None,
            },
        },
    )
    assert rerun["status"] == "already_tombstoned", rerun


# ─── kill mid-phase / phase-3 failure: one real lever ────────────────────────────────


def test_phase3_stuck_dlq_no_compensation_then_remediated_resume(
    rig: Any, lambda_client: Any, subject_all_eight: str
) -> None:
    _module, generator, config = rig
    handle = subject_all_eight
    saga_id = f"saga_{uuid.uuid4().hex[:12]}"
    manifest = build_fixture_manifest(saga_id=saga_id, subject_ref=handle)
    billing = next(p for p in manifest.participants if p.system_id == "billing-ledger")

    # The lever: a stale IN_FLIGHT claim on billing's hard_delete idempotency key.
    # The participant's own dedup then refuses to run — a genuine operational state
    # (a crashed prior attempt), reproduced deterministically.
    poison_key = idempotency_key(
        saga_id=saga_id,
        system_id="billing-ledger",
        operation=Verb.HARD_DELETE,
        artifacts=billing.artifacts,
    )
    ddb = boto3.client("dynamodb")
    now = int(time.time())
    ddb.put_item(
        TableName=f"asdp-{STAGE}-idempotency",
        Item={
            "system_id": {"S": "billing-ledger"},
            "idempotency_key": {"S": poison_key},
            "status": {"S": "IN_FLIGHT"},
            "claimed_at": {"N": str(now)},
            "expires_at": {"N": str(now + 3600)},
        },
    )
    try:
        started = _invoke(
            lambda_client,
            _EXECUTOR,
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
        stuck = _approve(lambda_client, saga_id, started["interrupt"]["manifestDigest"])
        assert stuck["status"] == "paused", stuck
        assert stuck["gate"] == "stuck", stuck
        assert stuck["interrupt"]["systemId"] == "billing-ledger"

        # The DLQ carries the failure.
        dlq_url = _stack_output("saga", "SagaDlqUrl")
        message = _drain_dlq_for(dlq_url, saga_id)
        assert message["systemId"] == "billing-ledger"

        # NO compensation happened: the Cognito user is still soft-deleted
        # (disabled), not restored — invariant 6 observed from the service side.
        user = generator._clients["cognito-idp"].admin_get_user(
            UserPoolId=config["userPoolId"], Username=handle
        )
        assert user["Enabled"] is False, "phase-3 failure must never restore"

        # Derived stores were already purged before billing stuck (§5.2 order).
        events = [e.event_type for e in _ledger_events(saga_id)]
        assert "PHASE3_STUCK" in events
        done = [
            e.body["systemId"]
            for e in _ledger_events(saga_id)
            if e.event_type == "HARD_DELETE_APPLIED"
        ]
        assert "vector-index" in done
        assert "billing-ledger" not in done
    finally:
        ddb.delete_item(
            TableName=f"asdp-{STAGE}-idempotency",
            Key={"system_id": {"S": "billing-ledger"}, "idempotency_key": {"S": poison_key}},
        )

    # ── remediated: operator resume finishes the arc with zero duplicates ────
    resumed = _invoke(
        lambda_client,
        _EXECUTOR,
        {"action": "resume", "thread_id": saga_id, "resume": {"wake_reason": "retry_phase3"}},
    )
    assert resumed["status"] == "paused", resumed
    assert resumed["gate"] == "sweep", resumed
    final = _run_sweeps_to_completion(lambda_client, saga_id)
    assert final["status"] == "completed", final

    entries = _ledger_events(saga_id)
    assert verify_chain(entries) == len(entries)
    applied = [e.body["systemId"] for e in entries if e.event_type == "HARD_DELETE_APPLIED"]
    assert sorted(applied) == sorted(set(applied)), "a system was hard-deleted twice"
    assert len(applied) == 8


# ─── phase-2 failure → full compensation ─────────────────────────────────────────────

_GHOST = ManifestParticipant(
    system_id="phantom-store",
    archetype=Archetype.OPERATIONAL_NOSQL,
    artifacts=(Artifact(kind="subject-data", locator="phantom-store:none"),),
    planned_ops=("soft_delete", "hard_delete"),
    order=OrderSlot(phase=3, rank=30),
)


def test_phase2_failure_compensates_fully(rig: Any, lambda_client: Any) -> None:
    module, generator, config = rig
    subset = ("cognito-identity", "profile-store")
    with _seeded_subject(rig, subset) as handle:
        saga_id = f"saga_{uuid.uuid4().hex[:12]}"
        manifest = build_fixture_manifest(
            saga_id=saga_id,
            subject_ref=handle,
            system_ids=subset,
            extra_participants=(_GHOST,),
        )
        started = _invoke(
            lambda_client,
            _EXECUTOR,
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
        # The ghost's soft delete fails inside the START invocation (no approval gate
        # is ever reached), and compensation runs to completion before it returns.
        assert started["status"] == "compensated", started

        user = generator._clients["cognito-idp"].admin_get_user(
            UserPoolId=config["userPoolId"], Username=handle
        )
        assert user["Enabled"] is True, "compensation must re-enable the identity"
        items = (
            generator._clients["dynamodb"]
            .Table(config["profileTable"])
            .query(
                KeyConditionExpression="subject_ref = :s",
                ExpressionAttributeValues={":s": handle},
                ConsistentRead=True,
            )
            .get("Items", [])
        )
        assert len(items) == module.PLACEMENTS["profile-store"]["items"]

        events = [e.event_type for e in _ledger_events(saga_id)]
        assert "SAGA_COMPENSATED" in events
        assert "HARD_DELETE_APPLIED" not in events


# ─── post-approval mutation → abort to re-approval ───────────────────────────────────


def test_approval_bound_to_a_different_digest_unwinds(rig: Any, lambda_client: Any) -> None:
    _module, generator, config = rig
    subset = ("cognito-identity", "profile-store")
    with _seeded_subject(rig, subset) as handle:
        saga_id = f"saga_{uuid.uuid4().hex[:12]}"
        manifest = build_fixture_manifest(saga_id=saga_id, subject_ref=handle, system_ids=subset)
        # The digest of a DIFFERENT plan — what a TOCTOU attacker would present.
        other = build_fixture_manifest(
            saga_id=saga_id, subject_ref=handle, system_ids=("cognito-identity",)
        )
        started = _invoke(
            lambda_client,
            _EXECUTOR,
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
        assert started["status"] == "paused"
        assert started["gate"] == "approval"
        assert other.digest != started["interrupt"]["manifestDigest"]

        result = _approve(lambda_client, saga_id, str(other.digest))
        assert result["status"] == "compensated", result

        events = [e.event_type for e in _ledger_events(saga_id)]
        assert "APPROVAL_INVALID" in events
        assert "HARD_DELETE_APPLIED" not in events
        user = generator._clients["cognito-idp"].admin_get_user(
            UserPoolId=config["userPoolId"], Username=handle
        )
        assert user["Enabled"] is True


# ─── helpers ──────────────────────────────────────────────────────────────────────────


def _stack_output(stack: str, key: str) -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=f"asdp-{STAGE}-{stack}")[
        "Stacks"
    ][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == key:
            return str(output["OutputValue"])
    raise KeyError(f"{key} not in asdp-{STAGE}-{stack} outputs")


def _drain_dlq_for(queue_url: str, saga_id: str) -> dict[str, Any]:
    """Find this saga's DLQ message, deleting what we read. Bounded wait."""
    sqs = boto3.client("sqs")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        received = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10
        )
        for message in received.get("Messages", []):
            body = json.loads(message["Body"])
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
            if body.get("sagaId") == saga_id:
                return dict(body)
    raise AssertionError(f"no DLQ message for {saga_id} within 60s")
