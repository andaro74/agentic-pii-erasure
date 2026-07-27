"""Digest-bound approval tokens (ADR-006) against moto's KMS.

moto exercises the minter's *logic* — envelope shape, binding checks, check order.
The real key's behaviour is the deployed gate's business, exactly as with the
manifest signer it shares a key with.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pii_erasure.approval.tokens import ApprovalTokenError, TokenMinter

_DIGEST = "sha256:" + "a" * 64
_OTHER = "sha256:" + "b" * 64


def _minter() -> tuple[TokenMinter, Any]:
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    kms = boto3.client("kms")
    key = kms.create_key(KeySpec="ECC_NIST_P256", KeyUsage="SIGN_VERIFY")["KeyMetadata"]
    return TokenMinter(key["KeyId"], client=kms), kms


def _mint(minter: TokenMinter) -> str:
    return minter.mint(
        manifest_digest=_DIGEST,
        saga_id="saga_1",
        approver="privacy-officer",
        approved_at="2026-07-26T12:00:00Z",
    )


@mock_aws
def test_mint_verify_round_trip() -> None:
    minter, _ = _minter()
    claim = minter.verify(_mint(minter), expected_digest=_DIGEST, saga_id="saga_1")
    assert claim["approver"] == "privacy-officer"


@mock_aws
def test_a_token_bound_to_a_different_digest_is_rejected_before_kms_is_asked() -> None:
    """The TOCTOU artefact (§8.3): valid signature, wrong plan. The binding check
    fires first — asserted by counting KMS calls, not by trusting the message."""
    minter, kms = _minter()
    token = _mint(minter)

    calls = {"verify": 0}
    original = kms.verify

    def _counting_verify(**kwargs: Any) -> Any:
        calls["verify"] += 1
        return original(**kwargs)

    kms.verify = _counting_verify
    with pytest.raises(ApprovalTokenError, match="different manifest digest"):
        minter.verify(token, expected_digest=_OTHER, saga_id="saga_1")
    assert calls["verify"] == 0, "binding must fail before cryptography is consulted"


@mock_aws
def test_a_token_for_a_different_saga_is_rejected() -> None:
    minter, _ = _minter()
    with pytest.raises(ApprovalTokenError, match="different saga"):
        minter.verify(_mint(minter), expected_digest=_DIGEST, saga_id="saga_2")


@mock_aws
def test_a_tampered_claim_fails_signature_verification() -> None:
    minter, _ = _minter()
    envelope = json.loads(base64.b64decode(_mint(minter)))
    envelope["claim"]["approver"] = "attacker"  # keep digest binding intact
    forged = base64.b64encode(json.dumps(envelope).encode()).decode()
    with pytest.raises(ApprovalTokenError, match="signature"):
        minter.verify(forged, expected_digest=_DIGEST, saga_id="saga_1")


@mock_aws
def test_a_manifest_signature_cannot_be_replayed_as_an_approval() -> None:
    """Same key signs both claim types; the `purpose` field is what keeps them
    apart. A forged envelope with the wrong purpose dies on that check."""
    minter, _ = _minter()
    envelope = json.loads(base64.b64decode(_mint(minter)))
    envelope["claim"]["purpose"] = "asdp-manifest"
    forged = base64.b64encode(json.dumps(envelope).encode()).decode()
    with pytest.raises(ApprovalTokenError, match="purpose"):
        minter.verify(forged, expected_digest=_DIGEST, saga_id="saga_1")


@mock_aws
def test_garbage_tokens_fail_loudly_not_cryptically() -> None:
    minter, _ = _minter()
    with pytest.raises(ApprovalTokenError, match="malformed"):
        minter.verify("not-base64!!", expected_digest=_DIGEST, saga_id="saga_1")
