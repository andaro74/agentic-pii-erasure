"""KMS asymmetric signing of the manifest digest (ARCHITECTURE §7.1, ADR-006).

The key is `ECC_NIST_P256` / `SIGN_VERIFY` — created in the foundation stack at M0 —
and the algorithm is pinned to `ECDSA_SHA_256` as a constant, not a parameter: a
configurable algorithm is a downgrade surface, and the key spec admits exactly one
sensible choice anyway.

**Why `MessageType="DIGEST"` always, not just above 4 KB.** `kms:Sign` accepts a raw
message only up to 4096 bytes; beyond that the caller must hash locally and say so. A
realistic eight-participant manifest clears 4 KB easily, so a RAW-mode implementation
works in every small test and fails on the first production-shaped plan — the trap the
roadmap names. We *always* hash locally (the digest already exists — it is the thing the
approval binds to) and always pass `DIGEST`, so there is one code path and it is the one
that works at any size.

Verification uses the key ARN recorded **in the signature block**, so a verifier checks
the signature against the key that made it — and `validate.py` is where a caller decides
whether that key is one it trusts.
"""

from __future__ import annotations

import base64
from typing import Any

import boto3

from pii_erasure.manifest.digest import assert_digest
from pii_erasure.manifest.models import Manifest, SignatureBlock

SIGNING_ALGORITHM = "ECDSA_SHA_256"


class SigningError(RuntimeError):
    """Signing or verification could not be completed honestly."""


def _digest_bytes(digest: str) -> bytes:
    prefix, _, hexpart = digest.partition(":")
    if prefix != "sha256" or len(hexpart) != 64:
        raise SigningError("digest is not sha256:<64 hex> — refusing to sign it")
    return bytes.fromhex(hexpart)


class ManifestSigner:
    """Sign and verify manifests against one KMS key.

    `key_id` may be a key id, ARN, or alias (``alias/asdp-<stage>-manifest-signing``).
    The client is injectable for the moto-backed unit tests; the deployed gate exercises
    the real key.
    """

    def __init__(self, key_id: str, *, client: Any | None = None) -> None:
        self._key_id = key_id
        self._kms = client or boto3.client("kms")

    def sign(self, manifest: Manifest) -> Manifest:
        """Sign the manifest's digest. Returns the signed manifest.

        Refuses a manifest that is already signed — immutability after signature is the
        rule (invariant 3), and re-signing an edited body is exactly the move it forbids.
        `replan()` is the sanctioned path to a changed plan.
        """
        if manifest.signature is not None:
            raise SigningError("manifest is already signed — re-plan, never re-sign")
        digest = assert_digest(manifest)  # raises if absent or stale

        response = self._kms.sign(
            KeyId=self._key_id,
            Message=_digest_bytes(digest),
            MessageType="DIGEST",
            SigningAlgorithm=SIGNING_ALGORITHM,
        )
        signature = SignatureBlock(
            kms_key_arn=response["KeyId"],  # KMS resolves aliases to the key ARN
            value=base64.b64encode(response["Signature"]).decode("ascii"),
        )
        return manifest.model_copy(update={"signature": signature})

    def verify(self, manifest: Manifest) -> None:
        """Verify digest AND signature. Raises `SigningError` on any failure.

        Order matters: the digest is recomputed from the body first, so a manifest whose
        body was edited after signing fails *here*, before KMS is ever asked — a valid
        signature over a stale digest must not read as a valid manifest.
        """
        if manifest.signature is None:
            raise SigningError("manifest is unsigned")
        digest = assert_digest(manifest)

        response = self._kms.verify(
            KeyId=manifest.signature.kms_key_arn,
            Message=_digest_bytes(digest),
            MessageType="DIGEST",
            Signature=base64.b64decode(manifest.signature.value),
            SigningAlgorithm=SIGNING_ALGORITHM,
        )
        if not response.get("SignatureValid"):
            raise SigningError("KMS reports the signature invalid for this digest")
