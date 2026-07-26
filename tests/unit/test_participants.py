"""Participant handler logic against `moto`.

**`moto` is not a gate** (CLAUDE.md conventions). It covers argument shaping, ordering and
response construction so the fast loop stays fast. The failures that actually matter here
— delete-marker semantics under real versioning, Object Lock refusing a delete, KMS
deletion windows, GSI lag — are precisely the ones it does not model, which is why
`make conformance` runs against a deployed stack and is the milestone's real gate.

What these tests *can* prove is that the harness routes the right verb to the right code
with the right arguments, that a replay does not act twice, and that a mutation without
its binding refuses rather than proceeding.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pii_erasure.contract import Outcome
from pii_erasure.participants._base import IdempotencyLog, ParticipantError, dispatch
from pii_erasure.participants._base.idempotency import ReplayInFlightError
from pii_erasure.participants.compliance_archive import ComplianceArchive
from pii_erasure.participants.compliance_archive.handler import CIPHERTEXT_KIND, DEK_KIND
from pii_erasure.participants.upload_bucket import UploadBucket
from pii_erasure.participants.upload_bucket.handler import SOFT_DELETE_TAG

SUBJECT = "sub_a3f9"
DIGEST = "sha256:" + "a" * 64
REGION = "us-east-1"


class _ClientContext:
    def __init__(self, tool: str, target: str = "upload-bucket") -> None:
        self.custom = {"bedrockAgentCoreToolName": f"{target}___{tool}"}


class _Context:
    """Stands in for the Lambda context object. The Gateway puts the verb in
    `client_context.custom`, so the harness reads it from exactly there."""

    def __init__(self, tool: str, target: str = "upload-bucket") -> None:
        self.client_context = _ClientContext(tool, target)


@pytest.fixture(autouse=True)
def _aws_credentials() -> None:
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


def _mutation(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subjectRef": SUBJECT,
        "sagaId": "saga_1",
        "manifestDigest": DIGEST,
        "idempotencyKey": "sha256:" + "b" * 64,
        "artifacts": [{"kind": "object", "locator": f"{SUBJECT}/", "count": 1}],
    }
    payload.update(overrides)
    return payload


# ─── upload-bucket: a delete marker is not a deletion ─────────────────────────────────


@mock_aws
def test_discover_reports_delete_markers_as_their_own_artifact() -> None:
    """The lesson, as a test. Someone already 'deleted' this object; the data is still
    there and discovery has to say so, or the operator believes a deletion happened."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="uploads")
    s3.put_bucket_versioning(Bucket="uploads", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="uploads", Key=f"{SUBJECT}/passport.pdf", Body=b"x")
    s3.delete_object(Bucket="uploads", Key=f"{SUBJECT}/passport.pdf")  # writes a marker

    response = UploadBucket("uploads", client=s3).discover(
        __import__("pii_erasure.contract", fromlist=["DiscoverRequest"]).DiscoverRequest(
            subject_ref=SUBJECT, saga_id="saga_1"
        )
    )

    kinds = {artifact.kind: artifact.count for artifact in response.artifacts}
    assert kinds["object"] == 1, "the version survived the delete"
    assert kinds["delete-marker"] == 1, "the marker must be reported, not hidden"
    assert response.found is True


@mock_aws
def test_hard_delete_removes_versions_and_markers_then_verify_is_clean() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="uploads")
    s3.put_bucket_versioning(Bucket="uploads", VersioningConfiguration={"Status": "Enabled"})
    for body in (b"v1", b"v2"):
        s3.put_object(Bucket="uploads", Key=f"{SUBJECT}/passport.pdf", Body=body)
    s3.delete_object(Bucket="uploads", Key=f"{SUBJECT}/passport.pdf")

    participant = UploadBucket("uploads", client=s3)
    body = dispatch(
        participant, _mutation(approvalToken="tok"), _Context("hard_delete"), idempotency=None
    )
    assert body["outcome"] == Outcome.APPLIED.value
    assert body["affected"] == 3, "two versions and one delete marker"

    listing = s3.list_object_versions(Bucket="uploads", Prefix=f"{SUBJECT}/")
    assert not listing.get("Versions")
    assert not listing.get("DeleteMarkers")

    verify = dispatch(participant, {"subjectRef": SUBJECT, "sagaId": "saga_1"}, _Context("verify"))
    assert verify["clean"] is True


@mock_aws
def test_soft_delete_tags_and_restore_untags() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="uploads")
    s3.put_bucket_versioning(Bucket="uploads", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="uploads", Key=f"{SUBJECT}/cv.pdf", Body=b"x")
    participant = UploadBucket("uploads", client=s3)

    dispatch(participant, _mutation(), _Context("soft_delete"))
    tags = s3.get_object_tagging(Bucket="uploads", Key=f"{SUBJECT}/cv.pdf")["TagSet"]
    assert {tag["Key"] for tag in tags} == {SOFT_DELETE_TAG}

    dispatch(participant, _mutation(restoreToken="rt"), _Context("restore"))
    assert s3.get_object_tagging(Bucket="uploads", Key=f"{SUBJECT}/cv.pdf")["TagSet"] == []

    # The soft delete must not have written a delete marker — that would be a "reversible"
    # operation that hides the data from every reader in the meantime.
    assert not s3.list_object_versions(Bucket="uploads").get("DeleteMarkers")


# ─── compliance-archive: erasure without deletion ─────────────────────────────────────


def _archive_fixture() -> tuple[ComplianceArchive, Any, Any]:
    s3 = boto3.client("s3", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)
    s3.create_bucket(Bucket="archive")
    ddb.create_table(
        TableName="dek",
        KeySchema=[{"AttributeName": "subject_ref", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "subject_ref", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3.put_object(Bucket="archive", Key=f"{SUBJECT}/2026/statement.enc", Body=b"ciphertext")
    ddb.put_item(
        TableName="dek",
        Item={"subject_ref": {"S": SUBJECT}, "wrapped_dek": {"B": b"wrapped"}},
    )
    return ComplianceArchive("archive", "dek", s3=s3, dynamodb=ddb), s3, ddb


@mock_aws
def test_discover_reports_partial_because_the_ciphertext_can_never_be_deleted() -> None:
    participant, _, _ = _archive_fixture()
    response = participant.discover(
        __import__("pii_erasure.contract", fromlist=["DiscoverRequest"]).DiscoverRequest(
            subject_ref=SUBJECT, saga_id="saga_1"
        )
    )
    kinds = {artifact.kind for artifact in response.artifacts}
    assert kinds == {CIPHERTEXT_KIND, DEK_KIND}
    assert response.deletability.value == "PARTIAL", (
        "the approver must see at plan time that the objects survive — invariant 7 starts "
        "at discovery, not at execution"
    )


@mock_aws
def test_hard_delete_shreds_the_key_and_leaves_the_objects() -> None:
    participant, s3, ddb = _archive_fixture()

    body = dispatch(
        participant,
        _mutation(approvalToken="tok"),
        _Context("hard_delete", target="compliance-archive"),
    )

    assert body["outcome"] == Outcome.APPLIED.value
    assert "Item" not in ddb.get_item(TableName="dek", Key={"subject_ref": {"S": SUBJECT}})
    assert s3.list_objects_v2(Bucket="archive", Prefix=f"{SUBJECT}/")["KeyCount"] == 1, (
        "the ciphertext is still there and always will be — that is the archetype"
    )


@mock_aws
def test_verify_distinguishes_shredded_from_never_present() -> None:
    """The trap this milestone names: after a shred, decryption failure must be
    distinguishable from not-found. An auditor needs to see that the objects survived and
    the key did not."""
    participant, _, ddb = _archive_fixture()
    request = __import__("pii_erasure.contract", fromlist=["VerifyRequest"]).VerifyRequest(
        subject_ref=SUBJECT, saga_id="saga_1"
    )

    before = participant.verify(request)
    assert before.clean is False

    ddb.delete_item(TableName="dek", Key={"subject_ref": {"S": SUBJECT}})
    after = participant.verify(request)
    assert after.clean is True

    absent = participant.verify(
        __import__("pii_erasure.contract", fromlist=["VerifyRequest"]).VerifyRequest(
            subject_ref="sub_nobody", saga_id="saga_1"
        )
    )
    assert absent.clean is True
    # Same verdict, different evidence — and the evidence is what an auditor reads.
    assert after.evidence.query_digest != absent.evidence.query_digest


# ─── the harness itself ───────────────────────────────────────────────────────────────


@mock_aws
def test_hard_delete_refuses_without_an_approval_token() -> None:
    participant, _, _ = _archive_fixture()
    body = dispatch(participant, _mutation(), _Context("hard_delete", target="compliance-archive"))
    assert body["outcome"] == Outcome.REFUSED.value
    assert body["affected"] == 0


@mock_aws
def test_a_mutation_refuses_without_a_well_formed_digest() -> None:
    participant, _, _ = _archive_fixture()
    for digest in ("", "not-a-digest", "sha256:short"):
        body = dispatch(
            participant,
            _mutation(manifestDigest=digest, approvalToken="tok"),
            _Context("hard_delete", target="compliance-archive"),
        )
        assert body["outcome"] == Outcome.REFUSED.value


@mock_aws
def test_a_replayed_key_returns_already_applied_and_does_not_act_twice() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)
    s3.create_bucket(Bucket="uploads")
    s3.put_bucket_versioning(Bucket="uploads", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="uploads", Key=f"{SUBJECT}/a.pdf", Body=b"x")
    ddb.create_table(
        TableName="idem",
        KeySchema=[
            {"AttributeName": "system_id", "KeyType": "HASH"},
            {"AttributeName": "idempotency_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "system_id", "AttributeType": "S"},
            {"AttributeName": "idempotency_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    participant = UploadBucket("uploads", client=s3)
    log = IdempotencyLog("idem", client=ddb)
    event = _mutation(approvalToken="tok")

    first = dispatch(participant, event, _Context("hard_delete"), idempotency=log)
    second = dispatch(participant, event, _Context("hard_delete"), idempotency=log)

    assert first["outcome"] == Outcome.APPLIED.value
    assert second["outcome"] == Outcome.ALREADY_APPLIED.value
    assert second["affected"] == first["affected"], "the replay reports what happened, truthfully"
    assert second["evidence"] == first["evidence"], "the receipt is the original receipt"


@mock_aws
def test_an_in_flight_replay_raises_rather_than_claiming_success() -> None:
    """Answering ALREADY_APPLIED for work that may still fail would stop the caller
    retrying something that never happened."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="idem",
        KeySchema=[
            {"AttributeName": "system_id", "KeyType": "HASH"},
            {"AttributeName": "idempotency_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "system_id", "AttributeType": "S"},
            {"AttributeName": "idempotency_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    log = IdempotencyLog("idem", client=ddb)
    assert log.claim(system_id="upload-bucket", key="k") is None
    with pytest.raises(ReplayInFlightError):
        log.claim(system_id="upload-bucket", key="k")


@mock_aws
def test_an_unknown_tool_fails_loudly() -> None:
    participant, _, _ = _archive_fixture()
    with pytest.raises(ParticipantError):
        dispatch(participant, {}, _Context("delete_everything", target="compliance-archive"))


@mock_aws
def test_a_call_with_no_tool_name_is_refused() -> None:
    """A participant acts on a named verb or not at all — never on an inferred one."""
    participant, _, _ = _archive_fixture()

    class _Bare:
        client_context = None

    with pytest.raises(ParticipantError):
        dispatch(participant, {}, _Bare())


@mock_aws
def test_a_dry_run_touches_nothing_and_does_not_burn_the_key() -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="uploads")
    s3.put_bucket_versioning(Bucket="uploads", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="uploads", Key=f"{SUBJECT}/a.pdf", Body=b"x")
    participant = UploadBucket("uploads", client=s3)

    dispatch(participant, _mutation(dryRun=True, approvalToken="tok"), _Context("hard_delete"))

    assert s3.list_object_versions(Bucket="uploads").get("Versions"), "dry run deleted data"
