"""Participants stack: one Lambda per AWS service, one IAM role each, least privilege.

Two participants land at M2 — the two whose archetypes break the word "delete":

* `upload-bucket` — S3 with versioning, where a delete marker hides data rather than
  removing it. Needs `s3:ListBucketVersions` and version-scoped deletes.
* `compliance-archive` — S3 Object Lock COMPLIANCE + KMS, where nothing can be deleted
  and erasure means destroying the per-subject DEK. Needs **no** `s3:DeleteObject` at
  all: its erasure path is a `DeleteItem` on the DEK registry.

**One role per participant, never a shared one** (ARCHITECTURE §9.3). A shared role would
make the blast radius of one compromised handler the union of eight services' permissions,
and would make "which participant did this" unanswerable from CloudTrail.

**No VPC configuration anywhere** — asserted at synth time. Aurora is reached through the
RDS Data API precisely so that no Lambda here needs one.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

#: Where `make package` stages the handler code and its dependencies. Committed empty so
#: `cdk synth` works with no build step — synth is part of the hermetic gate and must not
#: require Docker, a network, or a prior `pip install`.
#:
#: Anchored to this file rather than the working directory: `make synth` runs from
#: `infra/` and pytest runs from the repo root, and a relative asset path resolves
#: differently in each — which fails as "cannot find asset" in whichever one you did not
#: try first.
LAMBDA_ASSET = str(Path(__file__).resolve().parents[1] / "build" / "participants")

_RUNTIME = lambda_.Runtime.PYTHON_3_12


class ParticipantsStack(Stack):
    """The participant plane. Each function is a Gateway target; none of them reason."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        object_lock_days: int,
        dek_registry: dynamodb.ITable,
        idempotency: dynamodb.ITable,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        prod = stage == "prod"
        keep = RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY

        # ── The tenant wrapping CMK (ADR-007) ────────────────────────────────
        # Tenant-lifetime and symmetric: it wraps per-subject DEKs and outlives every
        # subject. It is NEVER the shred target — kms:ScheduleKeyDeletion's seven-day
        # minimum window would make hard_delete unable to return APPLIED inside a
        # one-month statutory deadline. The shred deletes the wrapped DEK instead.
        self.wrapping_key = kms.Key(
            self,
            "ArchiveWrappingKey",
            alias=f"asdp-{stage}-archive-wrapping",
            description="Wraps per-subject DEKs for compliance-archive. Not a shred target.",
            enable_key_rotation=True,
            removal_policy=keep,
        )

        # ── upload-bucket: versioning ON, which is the whole lesson ──────────
        self.upload_bucket = s3.Bucket(
            self,
            "UploadBucket",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=keep,
            auto_delete_objects=not prod,
        )

        # ── compliance-archive: COMPLIANCE-mode Object Lock ──────────────────
        # Undeletable by anyone, including root, until retention expires. Dev keeps the
        # window short or teardown is blocked — see infra/README.md, which leads with it.
        self.archive_bucket = s3.Bucket(
            self,
            "ComplianceArchive",
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

        self.upload_bucket_fn = self._function(
            "UploadBucketParticipant",
            stage=stage,
            system_id="upload-bucket",
            handler="pii_erasure.participants.upload_bucket.handler.lambda_handler",
            environment={
                "UPLOAD_BUCKET_NAME": self.upload_bucket.bucket_name,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        # Version-scoped: the participant lists and deletes *versions*, and never calls
        # DeleteObject, because that writes a delete marker and deletes nothing.
        self.upload_bucket.grant_read_write(self.upload_bucket_fn)
        self.upload_bucket_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObjectVersion", "s3:ListBucketVersions"],
                resources=[self.upload_bucket.bucket_arn, self.upload_bucket.arn_for_objects("*")],
            )
        )
        idempotency.grant_read_write_data(self.upload_bucket_fn)

        self.archive_fn = self._function(
            "ComplianceArchiveParticipant",
            stage=stage,
            system_id="compliance-archive",
            handler="pii_erasure.participants.compliance_archive.handler.lambda_handler",
            environment={
                "ARCHIVE_BUCKET_NAME": self.archive_bucket.bucket_name,
                "DEK_REGISTRY_TABLE": dek_registry.table_name,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        # READ ONLY on the bucket. Not an oversight — there is no delete to grant, and a
        # granted-but-impossible permission would suggest the erasure path runs through
        # S3 when it runs through the key registry.
        self.archive_bucket.grant_read(self.archive_fn)
        dek_registry.grant_read_write_data(self.archive_fn)
        idempotency.grant_read_write_data(self.archive_fn)
        self.wrapping_key.grant_encrypt_decrypt(self.archive_fn)

        for name, value in {
            "UploadBucketName": self.upload_bucket.bucket_name,
            "ComplianceArchiveBucketName": self.archive_bucket.bucket_name,
            "ArchiveWrappingKeyArn": self.wrapping_key.key_arn,
            "UploadBucketFunctionArn": self.upload_bucket_fn.function_arn,
            "ComplianceArchiveFunctionArn": self.archive_fn.function_arn,
        }.items():
            CfnOutput(self, name, value=value)

    def _function(
        self,
        construct_id: str,
        *,
        stage: str,
        system_id: str,
        handler: str,
        environment: dict[str, str],
    ) -> lambda_.Function:
        """One participant function. No VPC, ever — asserted at synth time."""
        return lambda_.Function(
            self,
            construct_id,
            function_name=f"asdp-{stage}-{system_id}",
            runtime=_RUNTIME,
            handler=handler,
            code=lambda_.Code.from_asset(LAMBDA_ASSET),
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={"PII_ERASURE_STAGE": stage, **environment},
            description=f"ASDP participant: {system_id}",
        )
