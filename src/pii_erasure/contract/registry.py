"""The participant registry — the eight real AWS services (ARCHITECTURE §4.2).

The conformance suite is parameterised over this tuple, so a new participant is covered
the moment it is registered here rather than when someone remembers to write tests for
it. That is the whole onboarding process: one Lambda, one Gateway target, passing
conformance.

Data only. No clients, no `boto3`, no framework — this module is imported by tests that
must run with no AWS account.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pii_erasure.contract.archetypes import Archetype


@dataclass(frozen=True)
class ParticipantSpec:
    """What the platform knows about a participant before it ever calls one."""

    system_id: str
    archetype: Archetype
    aws_service: str

    #: True when this participant **cannot** fully delete on a healthy path, so a
    #: correct `hard_delete` returns PARTIAL with a populated residual. Conformance
    #: asserts the honest outcome per participant rather than a single global
    #: expectation — invariant 7 is only meaningful if the exceptions are named.
    expects_residual: bool = False

    #: One line on what this participant exists to teach.
    lesson: str = field(default="")


PARTICIPANTS: tuple[ParticipantSpec, ...] = (
    ParticipantSpec(
        system_id="cognito-identity",
        archetype=Archetype.AUTHORITATIVE_IDENTITY,
        aws_service="Amazon Cognito",
        lesson="Revoke first — stop new writes before deleting old ones",
    ),
    ParticipantSpec(
        system_id="profile-store",
        archetype=Archetype.OPERATIONAL_NOSQL,
        aws_service="Amazon DynamoDB",
        lesson="GSIs are eventually consistent; a TTL is not a deletion guarantee",
    ),
    ParticipantSpec(
        system_id="billing-ledger",
        archetype=Archetype.RELATIONAL,
        aws_service="Aurora PostgreSQL Serverless v2 (RDS Data API)",
        lesson="Referential integrity dictates ordering; statutory retention beats erasure",
    ),
    ParticipantSpec(
        system_id="upload-bucket",
        archetype=Archetype.DELETABLE_BLOB,
        aws_service="Amazon S3 (versioning on)",
        lesson="A delete marker is not a deletion",
    ),
    ParticipantSpec(
        system_id="compliance-archive",
        archetype=Archetype.WORM,
        aws_service="Amazon S3 Object Lock COMPLIANCE + AWS KMS",
        lesson="Deletion redefined as irreversible loss of readability — shred the DEK",
    ),
    ParticipantSpec(
        system_id="vector-index",
        archetype=Archetype.DERIVED_INDEX,
        aws_service="Amazon S3 Vectors",
        lesson="An embedding outlives its source and is itself personal data",
    ),
    ParticipantSpec(
        system_id="analytics-lake",
        archetype=Archetype.COLUMNAR_ANALYTICS,
        aws_service="Amazon S3 + AWS Glue + Amazon Athena (Iceberg)",
        expects_residual=True,
        lesson="You cannot delete a row from a Parquet file — you rewrite or you shred",
    ),
    ParticipantSpec(
        system_id="notify-suppression",
        archetype=Archetype.RESIDUAL_BY_DESIGN,
        aws_service="Amazon SES",
        expects_residual=True,
        lesson="Some residual is legally required — disclose it, never hide it",
    ),
)

_BY_SYSTEM_ID: dict[str, ParticipantSpec] = {spec.system_id: spec for spec in PARTICIPANTS}


def get(system_id: str) -> ParticipantSpec:
    """Look up a participant, or fail loudly.

    An unknown `systemId` is never a shrug: it means a manifest names a system the
    platform cannot reach, and continuing would report an erasure that never happened.
    """
    try:
        return _BY_SYSTEM_ID[system_id]
    except KeyError:
        raise KeyError(
            f"unknown systemId {system_id!r} — not in the participant registry"
        ) from None


def system_ids() -> tuple[str, ...]:
    """Registered `systemId`s, in registry order (which is the order §4.2 documents)."""
    return tuple(spec.system_id for spec in PARTICIPANTS)
