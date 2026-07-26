"""Manifest digest, signing, and immutability — ADR-006's mechanism, tested from M3.

The two assertions that carry the milestone (ROADMAP M3, "hermetic done when"):

* mutate **any** meaningful field → the digest changes;
* change **only** provenance (session ID, trace ID, timestamps) → the digest is
  **identical**.

Everything else defends the edges: enumeration order, the digest's own exclusion from
the digested body, the KMS round-trip against moto, tamper detection ordered *before*
KMS, and `replan()` as the only door out of immutability.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from pii_erasure.contract import Archetype, Artifact, Hold, Residual
from pii_erasure.manifest import (
    Manifest,
    ManifestParticipant,
    ManifestSigner,
    ManifestValidationError,
    OrderSlot,
    Provenance,
    SigningError,
    assert_digest,
    compute_digest,
    digested_body,
    is_semantically_identical,
    replan,
    validate_manifest,
    with_digest,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "manifest"

_PROVENANCE = Provenance(
    discovered_at="2026-07-23T10:14:02Z",
    agent_version="asdp-discovery@2.3.1",
    model_id="anthropic.claude-sonnet-5",
    runtime_session_id="rs-0001",
    trace_id="1-abc-def",
)


def _participant(**overrides: Any) -> ManifestParticipant:
    payload: dict[str, Any] = {
        "system_id": "compliance-archive",
        "archetype": Archetype.WORM,
        "artifacts": (
            Artifact(kind="locked-object", locator="sub_a3f9/", count=12),
            Artifact(kind="wrapped-dek", locator="dek#sub_a3f9", count=1),
        ),
        "planned_ops": ("soft_delete", "hard_delete"),
        "order": OrderSlot(phase=3, rank=99),
        "delete_method": "CRYPTO_SHRED",
        "dek_registry_ref": "kr#sub_a3f9",
    }
    payload.update(overrides)
    return ManifestParticipant(**payload)  # type: ignore[arg-type]


def _upload(**overrides: Any) -> ManifestParticipant:
    payload: dict[str, Any] = {
        "system_id": "upload-bucket",
        "archetype": Archetype.DELETABLE_BLOB,
        "artifacts": (Artifact(kind="object", locator="sub_a3f9/", count=3),),
        "planned_ops": ("soft_delete", "hard_delete"),
        "order": OrderSlot(phase=3, rank=10),
    }
    payload.update(overrides)
    return ManifestParticipant(**payload)  # type: ignore[arg-type]


def _manifest(**overrides: Any) -> Manifest:
    payload: dict[str, Any] = {
        "manifest_id": "man_01JQ8TEST",
        "saga_id": "saga_01JQ8TEST",
        "subject_ref": "sub_a3f9",
        "request_id": "dsr_2026_0412",
        "provenance": _PROVENANCE,
        "participants": (_upload(), _participant()),
        "legal_holds": (),
        "residual_risk": (
            Residual(kind="hash", locator="ses:suppression", reason="legally retained"),
        ),
        "grace_window_days": 30,
    }
    payload.update(overrides)
    return Manifest(**payload)  # type: ignore[arg-type]


# ─── The two assertions the milestone names ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_ref", "sub_other"),
        ("saga_id", "saga_other"),
        ("request_id", "dsr_other"),
        ("grace_window_days", 7),
        ("schema_version", "9.9.9"),
        ("legal_holds", (Hold(hold_id="LIT-1", authority="Legal", scope="x", basis="17(3)(e)"),)),
        ("residual_risk", ()),
        ("participants", (_upload(),)),  # a system dropped from the plan
    ],
)
def test_mutating_any_meaningful_field_changes_the_digest(field: str, value: Any) -> None:
    baseline = compute_digest(_manifest())
    assert compute_digest(_manifest(**{field: value})) != baseline


def test_participant_level_changes_change_the_digest() -> None:
    baseline = compute_digest(_manifest())
    more_rows = _participant(
        artifacts=(
            Artifact(kind="locked-object", locator="sub_a3f9/", count=13),
            Artifact(kind="wrapped-dek", locator="dek#sub_a3f9", count=1),
        )
    )
    assert compute_digest(_manifest(participants=(_upload(), more_rows))) != baseline
    # Execution order is semantic: moving the shred earlier is a different plan (§8.3).
    resequenced = _participant(order=OrderSlot(phase=3, rank=1))
    assert compute_digest(_manifest(participants=(_upload(), resequenced))) != baseline


def test_planned_ops_order_is_semantic() -> None:
    forward = _manifest(participants=(_upload(planned_ops=("soft_delete", "hard_delete")),))
    reordered = _manifest(participants=(_upload(planned_ops=("hard_delete", "soft_delete")),))
    assert compute_digest(forward) != compute_digest(reordered)


def test_provenance_changes_do_not_change_the_digest() -> None:
    """The invariant-4 half: a re-run tomorrow, new session, new trace, new timestamp —
    same plan, same digest, same still-valid approval."""
    baseline = compute_digest(_manifest())
    rerun = _manifest(
        provenance=Provenance(
            discovered_at="2026-07-24T08:00:00Z",
            agent_version="asdp-discovery@2.4.0",
            model_id=None,
            runtime_session_id="rs-9999",
            trace_id=None,
        )
    )
    assert compute_digest(rerun) == baseline
    assert is_semantically_identical(_manifest(), rerun)


def test_enumeration_order_is_not_plan_identity() -> None:
    """Discovery returning the same estate in a different order must not churn the
    digest — participants sort by their order slot, holds by holdId."""
    one = _manifest(participants=(_upload(), _participant()))
    other = _manifest(participants=(_participant(), _upload()))
    assert compute_digest(one) == compute_digest(other)

    holds = (
        Hold(hold_id="LIT-2024-118", authority="Legal", scope="a", basis="17(3)(e)"),
        Hold(hold_id="LIT-2023-002", authority="Legal", scope="b", basis="17(3)(e)"),
    )
    assert compute_digest(_manifest(legal_holds=holds)) == compute_digest(
        _manifest(legal_holds=tuple(reversed(holds)))
    )


def test_attaching_the_digest_does_not_change_the_digest() -> None:
    """The circularity check. Quiet failure here would poison every approval."""
    manifest = _manifest()
    digested = with_digest(manifest)
    assert digested.digest == compute_digest(digested) == compute_digest(manifest)
    assert assert_digest(digested) == digested.digest


def test_the_digested_body_contains_no_volatile_keys() -> None:
    body = json.dumps(digested_body(_manifest()))
    for leaked in ("provenance", "runtimeSessionId", "traceId", "discoveredAt", "signature"):
        assert leaked not in body


# ─── Immutability: frozen, digest-checked, replan-only ────────────────────────────────


def test_manifests_are_frozen() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError):
        manifest.subject_ref = "sub_other"  # type: ignore[misc]


def test_an_edited_manifest_fails_validation() -> None:
    """`model_copy` can produce an edited manifest — and its stale digest betrays it."""
    edited = with_digest(_manifest()).model_copy(update={"subject_ref": "sub_other"})
    with pytest.raises(ManifestValidationError, match="modified after digesting"):
        validate_manifest(edited)


def test_replan_produces_a_successor_not_an_edit() -> None:
    signed = _sign_with_moto(with_digest(_manifest()))
    successor = replan(signed, manifest_id="man_01JQ8NEXT", grace_window_days=7)
    assert successor.manifest_id == "man_01JQ8NEXT"
    assert successor.digest is None, "the successor must earn its own digest"
    assert successor.signature is None, "…and its own signature, and its own approval"
    assert signed.signature is not None, "the original is untouched"


def test_replan_refuses_to_reuse_the_manifest_id() -> None:
    with pytest.raises(ManifestValidationError, match="NEW manifestId"):
        replan(_manifest(), manifest_id="man_01JQ8TEST")


def test_duplicate_participants_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate systemId"):
        _manifest(participants=(_upload(), _upload()))


def test_crypto_shred_requires_a_registry_ref() -> None:
    with pytest.raises(ValidationError, match="dekRegistryRef"):
        _participant(dek_registry_ref=None)


def test_unknown_planned_ops_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown plannedOps"):
        _upload(planned_ops=("soft_delete", "obliterate"))


# ─── KMS signing against moto ─────────────────────────────────────────────────────────


def _sign_with_moto(manifest: Manifest) -> Manifest:
    with mock_aws():
        signer, _ = _moto_signer()
        return signer.sign(manifest)


def _moto_signer() -> tuple[ManifestSigner, Any]:
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    kms = boto3.client("kms")
    key = kms.create_key(KeySpec="ECC_NIST_P256", KeyUsage="SIGN_VERIFY")["KeyMetadata"]
    return ManifestSigner(key["KeyId"], client=kms), kms


@mock_aws
def test_sign_verify_round_trip() -> None:
    signer, _ = _moto_signer()
    signed = signer.sign(with_digest(_manifest()))
    assert signed.signature is not None
    assert signed.signature.kms_key_arn.startswith("arn:aws:kms:")
    signer.verify(signed)  # raises on failure
    validate_manifest(
        signed,
        signer=signer,
        trusted_key_arns=frozenset({signed.signature.kms_key_arn}),
    )


@mock_aws
def test_signing_an_undigested_manifest_is_refused() -> None:
    signer, _ = _moto_signer()
    with pytest.raises(ValueError, match="no digest"):
        signer.sign(_manifest())


@mock_aws
def test_signing_twice_is_refused() -> None:
    signer, _ = _moto_signer()
    signed = signer.sign(with_digest(_manifest()))
    with pytest.raises(SigningError, match="re-plan, never re-sign"):
        signer.sign(signed)


@mock_aws
def test_a_body_edited_after_signing_fails_before_kms_is_asked() -> None:
    """Tamper detection must not depend on the signature check: a valid signature over a
    stale digest is still an invalid manifest."""
    signer, _ = _moto_signer()
    signed = signer.sign(with_digest(_manifest()))
    tampered = signed.model_copy(update={"grace_window_days": 0})
    with pytest.raises(ValueError, match="modified after digesting"):
        signer.verify(tampered)


@mock_aws
def test_a_forged_signature_fails_verification() -> None:
    signer, _ = _moto_signer()
    signed = signer.sign(with_digest(_manifest()))
    assert signed.signature is not None
    forged = signed.model_copy(
        update={
            "signature": signed.signature.model_copy(
                update={"value": base64.b64encode(b"not-a-real-signature").decode()}
            )
        }
    )
    with pytest.raises(SigningError):
        signer.verify(forged)


@mock_aws
def test_a_signature_from_an_untrusted_key_is_rejected_without_asking_kms() -> None:
    """Substitution: cryptographically valid, wrong key. The trusted-set check runs
    first, so an attacker's own key never gets a vote."""
    _, kms = _moto_signer()
    attacker_key = kms.create_key(KeySpec="ECC_NIST_P256", KeyUsage="SIGN_VERIFY")["KeyMetadata"]
    attacker = ManifestSigner(attacker_key["KeyId"], client=kms)
    signed_by_attacker = attacker.sign(with_digest(_manifest()))
    assert signed_by_attacker.signature is not None
    with pytest.raises(ManifestValidationError, match="outside the trusted set"):
        validate_manifest(
            signed_by_attacker,
            signer=attacker,
            trusted_key_arns=frozenset({"arn:aws:kms:us-east-1:123456789012:key/the-real-one"}),
        )


def test_a_signature_without_a_digest_cannot_be_constructed() -> None:
    from pii_erasure.manifest import SignatureBlock

    with pytest.raises(ValidationError, match="signs nothing"):
        _manifest(signature=SignatureBlock(kms_key_arn="arn:aws:kms:x", value="AAAA"))


# ─── Golden fixture: the manifest layer gets its own tripwire ─────────────────────────


def test_golden_manifest_digest() -> None:
    """Pins the digest for a committed manifest. Canonicalisation has its own fixtures;
    this one catches *manifest-layer* drift — a renamed field, a changed normalisation —
    which would silently invalidate every outstanding approval on deploy."""
    fixture = json.loads((FIXTURES / "example.json").read_text(encoding="utf-8"))
    manifest = Manifest.model_validate(fixture["manifest"])
    assert compute_digest(manifest) == fixture["digest"], (
        "the manifest digest changed for a byte-identical fixture — this is a breaking "
        "change to approval binding; bump MANIFEST_SCHEMA_VERSION and add a new fixture, "
        "do not edit this one"
    )
    body = digested_body(manifest)
    assert (
        hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        == (fixture["bodySha256SortedJson"])
    )
