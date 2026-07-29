"""The five-verb Deletion Participant Contract.

Depends on nothing. Everything else depends on it. Framework-free and cloud-free by
rule (invariant 0) — this package is one of the two a reader should be able to lift
wholesale, and it is what made two framework migrations and one cloud-native rewrite
cheap enough to do on the record.
"""

from pii_erasure.contract.archetypes import Archetype
from pii_erasure.contract.canonical import (
    SCHEMA_VERSION,
    CanonicalisationError,
    JSONValue,
    canonical,
)
from pii_erasure.contract.holds import (
    SUBJECT_WIDE_SCOPES,
    blocks,
    partition,
    unmatched_scopes,
)
from pii_erasure.contract.idempotency import IdempotencyKeyError, idempotency_key
from pii_erasure.contract.outcomes import Deletability, Outcome
from pii_erasure.contract.registry import PARTICIPANTS, ParticipantSpec
from pii_erasure.contract.verbs import (
    MUTATING_VERBS,
    READ_ONLY_VERBS,
    Artifact,
    ContractModel,
    DiscoverRequest,
    DiscoverResponse,
    DiscoveryEvidence,
    HardDeleteRequest,
    Hold,
    MutationRequest,
    MutationResponse,
    ReceiptEvidence,
    Residual,
    RestoreRequest,
    SoftDeleteRequest,
    Verb,
    VerifyRequest,
    VerifyResponse,
)

__all__ = [
    "MUTATING_VERBS",
    "PARTICIPANTS",
    "READ_ONLY_VERBS",
    "SCHEMA_VERSION",
    "SUBJECT_WIDE_SCOPES",
    "Archetype",
    "Artifact",
    "CanonicalisationError",
    "ContractModel",
    "Deletability",
    "DiscoverRequest",
    "DiscoverResponse",
    "DiscoveryEvidence",
    "HardDeleteRequest",
    "Hold",
    "IdempotencyKeyError",
    "JSONValue",
    "MutationRequest",
    "MutationResponse",
    "Outcome",
    "ParticipantSpec",
    "ReceiptEvidence",
    "Residual",
    "RestoreRequest",
    "SoftDeleteRequest",
    "Verb",
    "VerifyRequest",
    "VerifyResponse",
    "blocks",
    "canonical",
    "idempotency_key",
    "partition",
    "unmatched_scopes",
]
