"""M3's deployed gate: one signed manifest round-trips against the **real** CMK.

Everything the moto suite proves, this proves against the service — and the one thing
moto cannot prove at all: that the key the foundation stack actually created
(`ECC_NIST_P256` / `SIGN_VERIFY`, alias ``asdp-<stage>-manifest-signing``) accepts
`ECDSA_SHA_256` over `MessageType="DIGEST"` and round-trips a verify. A wrong key spec,
a wrong message type, or a missing key would all surface here and nowhere hermetic.

Run by a human: ``make integration`` (the target lights at M3 with this file and grows
the full saga suite at M5). Costs fractions of a cent per run.
"""

from __future__ import annotations

import os

import pytest

from pii_erasure.contract import Archetype, Artifact
from pii_erasure.manifest import (
    Manifest,
    ManifestParticipant,
    ManifestSigner,
    OrderSlot,
    Provenance,
    SigningError,
    validate_manifest,
    with_digest,
)

pytestmark = pytest.mark.integration

STAGE = os.environ.get("PII_ERASURE_STAGE", "dev")
KEY_ALIAS = f"alias/asdp-{STAGE}-manifest-signing"


def _fixture_manifest() -> Manifest:
    return Manifest(
        manifest_id="man_m3_deployed_gate",
        saga_id="saga_m3_deployed_gate",
        subject_ref="sub_fixture_m3",  # pseudonymous fixture handle — never real PII
        request_id="dsr_m3_gate",
        provenance=Provenance(discovered_at="2026-07-26T00:00:00Z", agent_version="fixture@m3"),
        participants=(
            ManifestParticipant(
                system_id="upload-bucket",
                archetype=Archetype.DELETABLE_BLOB,
                artifacts=(Artifact(kind="object", locator="sub_fixture_m3/", count=1),),
                planned_ops=("soft_delete", "hard_delete"),
                order=OrderSlot(phase=3, rank=10),
            ),
        ),
        grace_window_days=30,
    )


def test_signed_manifest_round_trips_against_the_real_cmk() -> None:
    signer = ManifestSigner(KEY_ALIAS)
    signed = signer.sign(with_digest(_fixture_manifest()))

    assert signed.signature is not None
    assert signed.signature.kms_key_arn.startswith("arn:aws:kms:"), (
        "KMS should resolve the alias to the key ARN in the signature block"
    )

    signer.verify(signed)
    validate_manifest(
        signed,
        signer=signer,
        trusted_key_arns=frozenset({signed.signature.kms_key_arn}),
    )


def test_the_real_key_rejects_a_tampered_body() -> None:
    signer = ManifestSigner(KEY_ALIAS)
    signed = signer.sign(with_digest(_fixture_manifest()))
    tampered = signed.model_copy(update={"grace_window_days": 0})
    with pytest.raises((SigningError, ValueError)):
        signer.verify(tampered)
