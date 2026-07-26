"""Foundation stack: KMS CMK, the DynamoDB tables, S3 buckets, EventBridge bus.

Lands at M0 so that the two most dangerous data-shape decisions in the system
are assertable from commit one:

- the **DEK registry** has point-in-time recovery explicitly DISABLED and joins
  no backup plan — a restore of that table un-shreds every subject deleted since
  the restore point (invariant 14, threat T9);
- the **ledger archive** bucket has Object Lock in COMPLIANCE mode — a mutable
  audit ledger is not an audit ledger (ADR-010).

The checkpoint table's schema is copied from the *installed*
langgraph-checkpoint-aws DynamoDBSaver source, not from memory (ROADMAP rule 3):
single-table design, string keys ``PK``/``SK``, TTL attribute ``ttl``, large
payloads offloaded to S3 via ``s3_offload_config``.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

_PITR_ON = dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)
# Explicit, not defaulted: the template must SHOW the intent so the synth
# assertion in tests/unit/test_foundation_synth.py has something to bite on.
_PITR_OFF = dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=False)


class FoundationStack(Stack):
    """Stateful primitives every later stack builds on. No compute lives here."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        object_lock_days: int,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        prod = stage == "prod"
        keep = RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY

        # ── Manifest signing key (M3 signs, Cedar-gated callers verify) ──────
        self.signing_key = kms.Key(
            self,
            "ManifestSigningKey",
            alias=f"asdp-{stage}-manifest-signing",
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            description="ASDP deletion-manifest signing key (ECDSA_SHA_256)",
            removal_policy=keep,
        )

        # ── Checkpoints: THE system of record (ADR-016) ──────────────────────
        self.checkpoints = dynamodb.Table(
            self,
            "Checkpoints",
            table_name=f"asdp-{stage}-checkpoints",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",  # life of saga + 90d, set by the saver
            point_in_time_recovery_specification=_PITR_ON,
            removal_policy=keep,
        )
        self.checkpoint_offload = s3.Bucket(
            self,
            "CheckpointOffload",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=keep,
            auto_delete_objects=not prod,
        )

        # ── Audit ledger + immutable archive (ADR-010) ───────────────────────
        self.ledger = dynamodb.Table(
            self,
            "Ledger",
            table_name=f"asdp-{stage}-ledger",
            partition_key=dynamodb.Attribute(name="saga_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,  # → Firehose → archive
            point_in_time_recovery_specification=_PITR_ON,
            removal_policy=keep,
        )
        # COMPLIANCE mode: nobody, including root, can delete under retention.
        # Dev stages keep object_lock_days SHORT or this bucket blocks teardown
        # until retention expires — that hazard is documented, not accidental
        # (infra/README.md). auto_delete stays on for dev so a destroy retried
        # after expiry succeeds.
        self.ledger_archive = s3.Bucket(
            self,
            "LedgerArchive",
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.compliance(
                Duration.days(object_lock_days)
            ),
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=keep,
            auto_delete_objects=not prod,
        )

        # ── Tombstone registry: outlives the subject data permanently ────────
        self.tombstones = dynamodb.Table(
            self,
            "Tombstones",
            table_name=f"asdp-{stage}-tombstones",
            partition_key=dynamodb.Attribute(
                name="subject_hash", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=_PITR_ON,
            removal_policy=keep,
        )

        # ── DEK registry: deliberately fragile (invariant 14, threat T9) ─────
        # PITR disabled ON PURPOSE. No stream, no replica, no backup selection,
        # no export. Deleting an item here IS the crypto-shred; any recovery
        # path for this table is a breach mechanism, not a safety net. Never
        # "fix" this to enabled — its absence is the safety property.
        self.dek_registry = dynamodb.Table(
            self,
            "DekRegistry",
            table_name=f"asdp-{stage}-dek-registry",
            partition_key=dynamodb.Attribute(
                name="subject_ref", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=_PITR_OFF,
            removal_policy=keep,
        )

        # ── Participant idempotency log (contract §4.3) ──────────────────────
        self.idempotency = dynamodb.Table(
            self,
            "Idempotency",
            table_name=f"asdp-{stage}-idempotency",
            partition_key=dynamodb.Attribute(name="system_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="idempotency_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",  # ≥ max saga lifetime + 90d
            point_in_time_recovery_specification=_PITR_OFF,
            removal_policy=keep,
        )

        # ── Event bus ────────────────────────────────────────────────────────
        self.bus = events.EventBus(self, "Bus", event_bus_name=f"asdp-{stage}")

        for name, value in {
            "SigningKeyArn": self.signing_key.key_arn,
            "CheckpointsTable": self.checkpoints.table_name,
            "CheckpointOffloadBucket": self.checkpoint_offload.bucket_name,
            "LedgerTable": self.ledger.table_name,
            "LedgerArchiveBucket": self.ledger_archive.bucket_name,
            "TombstonesTable": self.tombstones.table_name,
            "DekRegistryTable": self.dek_registry.table_name,
            "IdempotencyTable": self.idempotency.table_name,
            "EventBusName": self.bus.event_bus_name,
        }.items():
            CfnOutput(self, name, value=value)
