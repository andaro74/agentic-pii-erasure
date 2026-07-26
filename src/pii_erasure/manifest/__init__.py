"""The Deletion Manifest: models, digest, KMS signing, validation (M3).

Depends on `contract/` and nothing else. `models.py` additionally stays `boto3`-free —
it is the second of the two liftable files (invariant 0); the KMS edge lives in
`signing.py` alone.
"""

from pii_erasure.manifest.digest import (
    DigestMismatchError,
    assert_digest,
    compute_digest,
    digested_body,
    with_digest,
)
from pii_erasure.manifest.models import (
    MANIFEST_SCHEMA_VERSION,
    Manifest,
    ManifestParticipant,
    OrderSlot,
    Provenance,
    SignatureBlock,
)
from pii_erasure.manifest.signing import SIGNING_ALGORITHM, ManifestSigner, SigningError
from pii_erasure.manifest.validate import (
    ManifestValidationError,
    is_semantically_identical,
    replan,
    validate_manifest,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "SIGNING_ALGORITHM",
    "DigestMismatchError",
    "Manifest",
    "ManifestParticipant",
    "ManifestSigner",
    "ManifestValidationError",
    "OrderSlot",
    "Provenance",
    "SignatureBlock",
    "SigningError",
    "assert_digest",
    "compute_digest",
    "digested_body",
    "is_semantically_identical",
    "replan",
    "validate_manifest",
    "with_digest",
]
