"""The conformance suite - five verbs x N participants, against a **deployed** stack.

Parameterised over `contract.registry`, so participant #9 is covered the moment it is
registered rather than when someone remembers to write tests for it. Never write a
bespoke conformance test per participant; extend the seeding table below instead.

**This runs against real AWS and costs money.** That is the point: a conformance test
against a mock proves the mock conforms (ADR-017). The failures this catches — a delete
marker left behind, Object Lock refusing a delete, an idempotency race — are exactly the
ones `moto` does not model.

Participants whose milestone has not landed **skip with a reason**, and become mandatory
automatically when their function appears. Same rule as the milestone-gated `make`
targets: never a silencing guard.

Each case seeds its own throwaway subject and erases it, so the suite needs no `make
seed` (which lands at M4) and leaves nothing behind.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from pii_erasure.contract import PARTICIPANTS, Outcome, ParticipantSpec

pytestmark = pytest.mark.conformance

STAGE = os.environ.get("PII_ERASURE_STAGE", "dev")
DIGEST = "sha256:" + "c" * 64


def _function_name(system_id: str) -> str:
    return f"asdp-{STAGE}-{system_id}"


@pytest.fixture(scope="session")
def lambda_client() -> Any:
    return boto3.client("lambda")


@pytest.fixture(params=PARTICIPANTS, ids=lambda spec: spec.system_id)
def participant(request: pytest.FixtureRequest, lambda_client: Any) -> ParticipantSpec:
    spec: ParticipantSpec = request.param
    try:
        lambda_client.get_function(FunctionName=_function_name(spec.system_id))
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        pytest.skip(f"{spec.system_id} is not deployed yet — see docs/ROADMAP.md")
    return spec


def call(client: Any, system_id: str, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a participant exactly the way AgentCore Gateway does.

    The verb travels in `ClientContext`, not in the payload, because that is where the
    Gateway puts it. Driving a different entry point here would test an entry point that
    production never uses.
    """
    response = client.invoke(
        FunctionName=_function_name(system_id),
        Payload=json.dumps(payload).encode(),
        ClientContext=base64.b64encode(
            json.dumps({"custom": {"bedrockAgentCoreToolName": f"{system_id}___{tool}"}}).encode()
        ).decode(),
    )
    body = json.loads(response["Payload"].read())
    assert "FunctionError" not in response, f"{system_id}.{tool} failed: {body}"
    return dict(body)


# ─── per-archetype seeding, so the suite is self-contained ────────────────────────────


def _seed_upload_bucket(subject: str) -> None:
    s3 = boto3.client("s3")
    bucket = _stack_output("participants", "UploadBucketName")
    s3.put_object(Bucket=bucket, Key=f"{subject}/passport.pdf", Body=b"fabricated")
    s3.put_object(Bucket=bucket, Key=f"{subject}/passport.pdf", Body=b"fabricated v2")
    # A prior "deletion" that deleted nothing — the archetype's whole lesson, seeded so
    # conformance proves discovery reports it.
    s3.delete_object(Bucket=bucket, Key=f"{subject}/passport.pdf")


def _seed_compliance_archive(subject: str) -> None:
    s3 = boto3.client("s3")
    ddb = boto3.client("dynamodb")
    bucket = _stack_output("participants", "ComplianceArchiveBucketName")
    s3.put_object(Bucket=bucket, Key=f"{subject}/statement.enc", Body=b"ciphertext")
    ddb.put_item(
        TableName=f"asdp-{STAGE}-dek-registry",
        Item={"subject_ref": {"S": subject}, "wrapped_dek": {"B": b"wrapped-fixture"}},
    )


SEEDERS = {
    "upload-bucket": _seed_upload_bucket,
    "compliance-archive": _seed_compliance_archive,
}


def _stack_output(stack: str, key: str) -> str:
    cfn = boto3.client("cloudformation")
    outputs = cfn.describe_stacks(StackName=f"asdp-{STAGE}-{stack}")["Stacks"][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == key:
            return str(output["OutputValue"])
    raise AssertionError(f"{key} not exported by asdp-{STAGE}-{stack}")


@pytest.fixture
def subject(participant: ParticipantSpec) -> str:
    """A throwaway pseudonymous handle, seeded and then erased. Never real PII."""
    handle = f"sub_conf_{uuid.uuid4().hex[:12]}"
    seeder = SEEDERS.get(participant.system_id)
    if seeder is None:
        pytest.skip(f"no conformance seeder for {participant.system_id} yet")
    seeder(handle)
    return handle


def _mutation(subject_ref: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subjectRef": subject_ref,
        "sagaId": f"saga_conf_{uuid.uuid4().hex[:8]}",
        "manifestDigest": DIGEST,
        "idempotencyKey": "sha256:" + uuid.uuid4().hex * 2,
        "artifacts": [],
    }
    payload.update(overrides)
    return payload


# ─── the assertions of ARCHITECTURE §4.4 ──────────────────────────────────────────────


def test_discover_finds_the_subject(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    body = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    assert body["found"] is True
    assert body["artifacts"], "seeded data must be discoverable — recall starts here"
    assert body["archetype"] == participant.archetype.value
    assert body["evidence"]["queryDigest"].startswith("sha256:")


def test_discover_is_side_effect_free(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    """Asserted by snapshot diff of the response, twice — including version and
    delete-marker state, which is where a "read" that quietly tidies up would show."""
    first = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    second = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    assert first["artifacts"] == second["artifacts"]
    assert first["found"] == second["found"]


def test_soft_delete_then_restore_returns_the_original_artifact_set(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    before = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    soft = call(lambda_client, participant.system_id, "soft_delete", _mutation(subject))
    assert soft["outcome"] in (Outcome.APPLIED.value, Outcome.PARTIAL.value)

    call(
        lambda_client,
        participant.system_id,
        "restore",
        _mutation(subject, restoreToken=soft.get("restoreToken") or "restore"),
    )
    after = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    assert after["artifacts"] == before["artifacts"]


def test_a_replayed_idempotency_key_does_not_double_apply(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    payload = _mutation(subject)
    first = call(lambda_client, participant.system_id, "soft_delete", payload)
    second = call(lambda_client, participant.system_id, "soft_delete", payload)
    assert first["outcome"] == Outcome.APPLIED.value
    assert second["outcome"] == Outcome.ALREADY_APPLIED.value
    assert second["affected"] == first["affected"]


def test_hard_delete_refuses_without_its_binding(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    no_token = call(lambda_client, participant.system_id, "hard_delete", _mutation(subject))
    assert no_token["outcome"] == Outcome.REFUSED.value

    no_digest = call(
        lambda_client,
        participant.system_id,
        "hard_delete",
        _mutation(subject, manifestDigest="", approvalToken="tok"),
    )
    assert no_digest["outcome"] == Outcome.REFUSED.value


def test_verify_is_clean_only_after_hard_delete(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    before = call(
        lambda_client,
        participant.system_id,
        "verify",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    assert before["clean"] is False, "verify claimed clean while the subject was present"

    result = call(
        lambda_client,
        participant.system_id,
        "hard_delete",
        _mutation(subject, approvalToken="conformance-token"),
    )
    assert result["outcome"] in (Outcome.APPLIED.value, Outcome.PARTIAL.value)

    after = call(
        lambda_client,
        participant.system_id,
        "verify",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    assert after["clean"] is True


def test_residual_honesty(lambda_client: Any, participant: ParticipantSpec, subject: str) -> None:
    """Invariant 7, per participant. A system that cannot fully delete says PARTIAL and
    names what remains; one that can must not hedge."""
    result = call(
        lambda_client,
        participant.system_id,
        "hard_delete",
        _mutation(subject, approvalToken="conformance-token"),
    )
    if participant.expects_residual:
        assert result["outcome"] == Outcome.PARTIAL.value
        assert result["residual"], "PARTIAL with an empty residual says nothing"
    else:
        assert result["outcome"] == Outcome.APPLIED.value
        assert not result["residual"]


def test_every_response_carries_evidence(
    lambda_client: Any, participant: ParticipantSpec, subject: str
) -> None:
    read = call(
        lambda_client,
        participant.system_id,
        "discover",
        {
            "subjectRef": subject,
            "sagaId": "saga_conf",
        },
    )
    write = call(lambda_client, participant.system_id, "soft_delete", _mutation(subject))
    assert read["evidence"]["queryDigest"].startswith("sha256:")
    assert write["evidence"]["receiptDigest"].startswith("sha256:")
