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

Each case seeds its own throwaway subject and tears it down afterwards, so the suite
needs no `make seed` and repeated runs do not accumulate residue (V8-13). The claim is
scoped honestly rather than absolutely: ciphertext in the **Object Lock** bucket cannot
be deleted by anyone — including this teardown — until the dev retention window expires,
which is the WORM archetype asserting itself on its own test rig; and the idempotency
log keeps its pseudonymous receipts, which carry no subject data. Everything addressable
is removed.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from evals.fixtures.generator import (
    FixtureGenerator,
    SesSandboxError,
    _purge_versions,
    _safe_handle,
)
from pii_erasure.cli.main import _seed_clients, _stack_config
from pii_erasure.contract import PARTICIPANTS, Outcome, ParticipantSpec
from pii_erasure.participants.billing_ledger import handler as billing
from pii_erasure.participants.billing_ledger.handler import execute_with_resume
from pii_erasure.participants.vector_index.handler import vector_key

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


# ─── per-archetype seeding and teardown, so the suite is self-contained ───────────────
# Seeding reuses the ground-truth generator's writers — the same code path `make seed`
# exercises, already proven against the deployed services and already measuring what it
# wrote (V8-12). A bespoke conformance seeder would be a second implementation free to
# drift from the one the recall gate trusts.

#: What one throwaway subject gets in each system. Every registered participant MUST
#: have an entry — asserted hermetically by tests/unit/test_conformance_coverage.py —
#: so registering participant #9 without extending this table fails `make check`
#: instead of silently skipping. A skip that reads as coverage is V8-3's shape.
PLACEMENTS: dict[str, dict[str, int]] = {
    "cognito-identity": {"users": 1},
    "profile-store": {"items": 2},
    "billing-ledger": {"customers": 1, "invoices": 1, "invoice_lines": 1},
    "upload-bucket": {"objects": 2, "deleteMarkers": 1},
    "compliance-archive": {"lockedObjects": 1, "wrappedDeks": 1},
    "vector-index": {"vectors": 3},
    "analytics-lake": {"rows": 2},
    "notify-suppression": {"contacts": 1, "suppressionEntries": 1},
}


@pytest.fixture(scope="session")
def rig() -> tuple[FixtureGenerator, dict[str, str]]:
    """One generator against the deployed stack, config resolved from its outputs.

    `allow_ses_sandbox=False` deliberately: in a sandbox account the notify-suppression
    seeder raises, and the fixture converts that into a *reasoned skip* — a capability
    gate that goes mandatory the moment production access lands, not a silencing guard.
    """
    config = _stack_config(os.environ.get("PII_ERASURE_TENANT", "meridian"))
    return FixtureGenerator(clients=_seed_clients(), config=config), config


@pytest.fixture
def subject(
    participant: ParticipantSpec, rig: tuple[FixtureGenerator, dict[str, str]]
) -> Iterator[str]:
    """A throwaway pseudonymous handle: seeded, tested against, then torn down. Never real PII."""
    generator, config = rig
    handle = f"sub_conf_{uuid.uuid4().hex[:12]}"
    throwaway = {
        "subjectRef": handle,
        "displayName": "Conformance Fixture",
        "email": f"{handle}@meridian.invalid",
    }
    try:
        generator._writers()[participant.system_id](throwaway, PLACEMENTS[participant.system_id])
    except SesSandboxError:
        # The contact seeded before the suppression write was refused — clean it, or the
        # skip path itself leaks (V8-13's shape, one layer down).
        _cleanup(participant.system_id, handle, generator, config)
        pytest.skip(
            "SES sandbox: the suppression entry cannot exist, so this archetype's "
            "residual contract cannot be exercised. Request production access (V8-11); "
            "these tests become mandatory the moment it lands."
        )
    yield handle
    _cleanup(participant.system_id, handle, generator, config)


def _cleanup(
    system_id: str, handle: str, generator: FixtureGenerator, config: dict[str, str]
) -> None:
    """Remove the throwaway subject's data everywhere it is removable (V8-13).

    Direct AWS calls, never the participant's own verbs: tearing down through the system
    under test would make cleanup depend on the very behaviour the test just judged.
    Failures here raise — a teardown that swallows its own errors reintroduces the
    residue this exists to stop, silently.
    """
    clients = generator._clients
    if system_id == "cognito-identity":
        try:
            clients["cognito-idp"].admin_delete_user(
                UserPoolId=config["userPoolId"], Username=handle
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "UserNotFoundException":
                raise
    elif system_id == "profile-store":
        table = clients["dynamodb"].Table(config["profileTable"])
        items = table.query(
            KeyConditionExpression="subject_ref = :s",
            ExpressionAttributeValues={":s": handle},
            ConsistentRead=True,
        ).get("Items", [])
        for item in items:
            table.delete_item(Key={"subject_ref": handle, "item_id": item["item_id"]})
    elif system_id == "billing-ledger":
        # The participant's own statements, child before parent — cleanup provably
        # targets the same tables the verbs do, and cannot drift from them.
        for table_name in billing._DELETE_ORDER:
            execute_with_resume(
                clients["rds-data"],
                resourceArn=config["billingClusterArn"],
                secretArn=config["billingSecretArn"],
                database=config["billingDatabase"],
                sql=billing._DELETE_SQL[table_name],
                parameters=[{"name": "subject_ref", "value": {"stringValue": handle}}],
            )
    elif system_id == "upload-bucket":
        _purge_versions(clients["s3"], config["uploadBucket"], f"{handle}/")
    elif system_id == "compliance-archive":
        # The DEK is the addressable half; shredding it is the archetype's own erasure
        # mechanism. The ciphertext is COMPLIANCE-locked and undeletable by anyone until
        # the dev retention window (1 day) expires — that impossibility IS the archetype,
        # and pretending to delete it would test a world that does not exist.
        clients["dynamodb"].Table(config["dekRegistryTable"]).delete_item(
            Key={"subject_ref": handle}
        )
    elif system_id == "vector-index":
        clients["s3vectors"].delete_vectors(
            vectorBucketName=config["vectorBucket"],
            indexName=config["vectorIndex"],
            keys=[vector_key(handle, n) for n in range(PLACEMENTS["vector-index"]["vectors"])],
        )
    elif system_id == "analytics-lake":
        generator._athena(
            f"DELETE FROM {config['analyticsTable']} WHERE subject_ref = '{_safe_handle(handle)}'"
        )
    elif system_id == "notify-suppression":
        ses = clients["sesv2"]
        for call in (
            lambda: ses.delete_contact(
                ContactListName=config["contactList"],
                EmailAddress=f"{handle}@meridian.invalid",
            ),
            # The test runner's credentials CAN delete a suppression entry — only the
            # participant's role is denied it (invariant 7 as IAM). Teardown is exactly
            # the case that permission split exists to allow.
            lambda: ses.delete_suppressed_destination(EmailAddress=f"{handle}@meridian.invalid"),
        ):
            try:
                call()
            except ClientError as error:
                if error.response["Error"]["Code"] not in (
                    "NotFoundException",
                    "BadRequestException",
                ):
                    raise
    else:  # pragma: no cover - the coverage guard makes this unreachable
        raise AssertionError(f"no cleanup for {system_id!r} — extend _cleanup")


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

    if not participant.expects_residual:
        assert after["clean"] is True
        return

    # Residual by design: `clean` must stay False, because something genuinely remains
    # (V8-3). Asserting clean=True here would have required these two participants to
    # claim an erasure they had not performed — the precise dishonesty invariant 7
    # exists to forbid, demanded by the suite that enforces invariant 7.
    #
    # The replacement is stricter, not weaker: it cross-checks two verbs against each
    # other, so a participant that discloses a residual in `hard_delete` and then
    # forgets it in `verify` now fails. "Nothing remains" and "what remains is exactly
    # what I disclosed" are both falsifiable; only the second is true here.
    assert after["clean"] is False, (
        f"{participant.system_id} claimed clean while its disclosed residual remains"
    )
    assert after["remaining"], "clean=False must name what is still there"
    assert {(item["kind"], item["locator"]) for item in after["remaining"]} == {
        (item["kind"], item["locator"]) for item in result["residual"]
    }, "verify and hard_delete disagree about what survived"


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
