"""Manifest validation and the one sanctioned way to change a signed plan.

Immutability after signature is enforced three ways, none sufficient alone:

1. **Frozen models** — attribute assignment raises. Stops accidents, not intent.
2. **The digest** — `model_copy` can still produce an edited manifest, but its recorded
   digest no longer matches its body, and `validate_manifest` refuses it. Stops edits,
   detectably.
3. **`replan()`** — the sanctioned path. A changed plan is a *new* manifest with a new
   `manifestId`, no digest, and no signature, which therefore requires a fresh digest,
   a fresh signature, and — the point of the whole arrangement — a fresh approval
   (invariant 3, ADR-006).
"""

from __future__ import annotations

from typing import Any

from pii_erasure.manifest.digest import assert_digest, compute_digest
from pii_erasure.manifest.models import MANIFEST_SCHEMA_VERSION, Manifest
from pii_erasure.manifest.signing import ManifestSigner


class ManifestValidationError(ValueError):
    """The manifest cannot be trusted as-is. The message says why; it never says PII."""


def validate_manifest(
    manifest: Manifest,
    *,
    signer: ManifestSigner | None = None,
    trusted_key_arns: frozenset[str] | None = None,
) -> None:
    """Validate shape, digest, and — when a signer is provided — the signature.

    `trusted_key_arns` closes the substitution hole: a cryptographically valid signature
    from an attacker-controlled key must not validate, so when the caller knows which
    key(s) may sign manifests, the signature's key is checked against that set before
    KMS is asked anything.
    """
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError(
            f"unsupported manifest schemaVersion {manifest.schema_version!r} "
            f"(this build understands {MANIFEST_SCHEMA_VERSION})"
        )
    try:
        assert_digest(manifest)
    except ValueError as error:
        raise ManifestValidationError(str(error)) from error

    if manifest.signature is not None:
        if trusted_key_arns is not None and manifest.signature.kms_key_arn not in trusted_key_arns:
            raise ManifestValidationError(
                "manifest is signed by a key outside the trusted set — a valid signature "
                "from the wrong key is not a valid manifest"
            )
        if signer is not None:
            try:
                signer.verify(manifest)
            except Exception as error:
                raise ManifestValidationError(f"signature verification failed: {error}") from error


def replan(manifest: Manifest, *, manifest_id: str, **updates: Any) -> Manifest:
    """Produce the successor manifest. Never mutates; never carries the old approval.

    The new manifest starts unsigned and undigested — whatever changed (or didn't:
    a re-plan after a denied approval may be identical in body), the successor must earn
    its own digest, signature, and approval.
    """
    if manifest_id == manifest.manifest_id:
        raise ManifestValidationError(
            "replan requires a NEW manifestId — reusing the old one is an edit in disguise"
        )
    return manifest.model_copy(
        update={**updates, "manifest_id": manifest_id, "digest": None, "signature": None}
    )


def is_semantically_identical(one: Manifest, other: Manifest) -> bool:
    """True when the two plans would digest identically — approval-equivalence.

    Used by tests and, later, by the presenter to say "this re-plan changes nothing".
    Compares the *computed* digests, not the recorded ones, so it answers about the
    bodies rather than about bookkeeping.
    """
    return compute_digest(one) == compute_digest(other)
