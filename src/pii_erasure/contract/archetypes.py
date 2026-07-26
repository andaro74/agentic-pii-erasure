"""The eight participant archetypes (ARCHITECTURE §4.2).

An archetype is not a taxonomy for its own sake: it names *how deletion behaves* in a
class of system, and each one in this list is backed by a real AWS service that genuinely
behaves that way ([ADR-017](../../../docs/adr/ADR-017-real-aws-participants.md)). Two of
them cannot honour a deletion request in the way the word implies, which is the point.
"""

from __future__ import annotations

from enum import Enum


class Archetype(str, Enum):
    """How a participating system loses data, not what technology it is built on."""

    #: Cognito. Revoke first — stop new writes before deleting old ones.
    AUTHORITATIVE_IDENTITY = "AUTHORITATIVE_IDENTITY"

    #: DynamoDB. GSIs are eventually consistent; a TTL is not a deletion guarantee.
    OPERATIONAL_NOSQL = "OPERATIONAL_NOSQL"

    #: Aurora via the RDS Data API. Referential integrity dictates ordering, and
    #: statutory retention beats erasure.
    RELATIONAL = "RELATIONAL"

    #: S3 with versioning. A delete marker is not a deletion.
    DELETABLE_BLOB = "DELETABLE_BLOB"

    #: S3 Object Lock COMPLIANCE + KMS. Undeletable by anyone, including root, until
    #: retention expires: deletion is redefined as irreversible loss of readability, so
    #: hard_delete shreds the per-subject DEK (ADR-007).
    WORM = "WORM"

    #: S3 Vectors. An embedding outlives its source and *is* personal data. No
    #: delete-by-query — keys are derived from the subjectRef (ADR-021).
    DERIVED_INDEX = "DERIVED_INDEX"

    #: Glue/Athena over Iceberg. You cannot delete a row from a Parquet file; you
    #: rewrite it or you shred it, and rows survive until snapshot expiry.
    COLUMNAR_ANALYTICS = "COLUMNAR_ANALYTICS"

    #: SES. Some residual is legally required — the suppression entry stays. Disclose
    #: it, never hide it (invariant 7).
    RESIDUAL_BY_DESIGN = "RESIDUAL_BY_DESIGN"
