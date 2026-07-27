"""Digest-bound approval tokens (ADR-006) — the mitigation for the TOCTOU attack.

The attack (ARCHITECTURE §8.3): the approver reviews manifest v1; the plan changes;
execution proceeds under v1's approval against v2's blast radius. The mitigation: the
token carries the approved digest and a KMS signature over the whole claim, so a token
is only ever valid for the exact bytes the human saw.

The signing key is the same asymmetric CMK that signs manifests — one key, two claim
types, distinguished by a `purpose` field inside the signed body so a manifest
signature can never be replayed as an approval token or vice versa.

Verification recomputes the claim digest locally and asks KMS to verify the signature.
Cedar-side enforcement of `token.digest == manifestDigest` at the Gateway lands at M6;
until then the saga itself and the participant precheck are the consumers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import boto3

_SIGNING_ALGORITHM = "ECDSA_SHA_256"
_PURPOSE = "asdp-approval-token"


class ApprovalTokenError(ValueError):
    """The token is malformed, unsigned, or bound to a different digest."""


def _claim_digest(claim: dict[str, Any]) -> bytes:
    text = json.dumps(claim, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).digest()


class TokenMinter:
    """Mint and verify approval tokens against one KMS key.

    `key_id` may be a key id, ARN, or alias — the same spec `ManifestSigner` takes,
    and in deployment the same key.
    """

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        self._key_id = key_id
        self._kms = client or boto3.client("kms")

    def mint(self, *, manifest_digest: str, saga_id: str, approver: str, approved_at: str) -> str:
        claim = {
            "purpose": _PURPOSE,
            "manifestDigest": manifest_digest,
            "sagaId": saga_id,
            "approver": approver,
            "approvedAt": approved_at,
        }
        response = self._kms.sign(
            KeyId=self._key_id,
            Message=_claim_digest(claim),
            MessageType="DIGEST",
            SigningAlgorithm=_SIGNING_ALGORITHM,
        )
        envelope = {
            "claim": claim,
            "kmsKeyArn": response["KeyId"],
            "signature": base64.b64encode(response["Signature"]).decode("ascii"),
        }
        return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")

    def verify(self, token: str, *, expected_digest: str, saga_id: str) -> dict[str, Any]:
        """Verify signature and binding. Returns the claim on success.

        Binding is checked *before* KMS is asked: a valid signature over the wrong
        digest is precisely the artefact the TOCTOU attack would present, and it must
        fail on the binding, loudly, not on cryptography.
        """
        try:
            envelope = json.loads(base64.b64decode(token, validate=True))
            claim = envelope["claim"]
            signature = base64.b64decode(envelope["signature"])
            key_arn = envelope["kmsKeyArn"]
        except (ValueError, KeyError, TypeError) as error:
            raise ApprovalTokenError(f"malformed approval token: {error}") from error

        if claim.get("purpose") != _PURPOSE:
            raise ApprovalTokenError("token purpose is not an approval — possible replay")
        if claim.get("manifestDigest") != expected_digest:
            raise ApprovalTokenError(
                "token is bound to a different manifest digest — the plan changed after "
                "approval, and execution under the old approval is forbidden (invariant 3)"
            )
        if claim.get("sagaId") != saga_id:
            raise ApprovalTokenError("token was minted for a different saga")

        response = self._kms.verify(
            KeyId=key_arn,
            Message=_claim_digest(claim),
            MessageType="DIGEST",
            Signature=signature,
            SigningAlgorithm=_SIGNING_ALGORITHM,
        )
        if not response.get("SignatureValid"):
            raise ApprovalTokenError("KMS reports the token signature invalid")
        return dict(claim)
