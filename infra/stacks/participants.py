"""Participants stack: one Lambda per AWS service, one IAM role each, least privilege.

All eight archetypes are here as of M4. Each is a real service that genuinely behaves the
way its archetype claims — that is ADR-017's whole argument, and the reason the stack is
this large.

**One role per participant, never a shared one** (ARCHITECTURE §9.3). A shared role would
make the blast radius of one compromised handler the union of eight services' permissions,
and would make "which participant did this" unanswerable from CloudTrail.

**Two permissions are withheld on purpose**, and both are load-bearing:

* `compliance-archive` gets **no** `s3:DeleteObject`. There is nothing to grant — Object
  Lock COMPLIANCE refuses deletion from everyone including root — and a
  granted-but-impossible permission would imply the erasure path runs through S3 when it
  runs through the DEK registry.
* `notify-suppression` gets **no** `ses:DeleteSuppressedDestination`. Invariant 7 says the
  suppression entry must survive erasure; this makes that a property of the role rather
  than a promise made by the handler. A future edit that tried to delete it would fail with
  `AccessDenied` instead of quietly turning a disclosed residual into a silent breach of
  the subject's opt-out.

**No Lambda attaches to a VPC** — asserted at synth time. There *is* a VPC here, because
Aurora cannot exist without one, and the repo previously claimed there was none anywhere
(V8-4, [ADR-023](../../docs/adr/ADR-023-aurora-needs-a-vpc.md)). It holds the cluster and
nothing else: isolated subnets, no NAT gateway, no internet gateway, no endpoints — none of
the parts that bill for existing. Every participant reaches its service over a public
SigV4 endpoint, Aurora included, via the RDS Data API.

Aurora Serverless v2 sits at `min_capacity = 0` ACU so an idle stack bills no compute
(ADR-021's rule, applied beyond the vector store).
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from aws_cdk import aws_ses as ses
from constructs import Construct

#: Imported, not restated: a vector written at a different width than the index declares is
#: rejected at write time, so the participant and the index must agree by construction.
from pii_erasure.participants.vector_index.handler import VECTOR_DIMENSION

#: Must match `analytics_lake.handler.SNAPSHOT_RETENTION_DAYS`. Asserted by a unit test —
#: a disclosed window the table does not honour would be a fabricated reassurance.
SNAPSHOT_RETENTION_DAYS = 7

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

        self._build_cognito_identity(stage, idempotency)
        self._build_profile_store(stage, idempotency, keep=keep)
        self._build_billing_ledger(stage, idempotency, keep=keep)
        self._build_vector_index(stage, idempotency, keep=keep)
        self._build_analytics_lake(stage, idempotency, keep=keep, prod=prod)
        self._build_notify_suppression(stage, idempotency)

        #: What the Gateway stack registers as MCP targets, and what the conformance
        #: suite resolves by name. Keyed by `systemId` so it lines up with the registry.
        self.functions: dict[str, lambda_.IFunction] = {
            "upload-bucket": self.upload_bucket_fn,
            "compliance-archive": self.archive_fn,
            "cognito-identity": self.cognito_fn,
            "profile-store": self.profile_fn,
            "billing-ledger": self.billing_fn,
            "vector-index": self.vector_fn,
            "analytics-lake": self.analytics_fn,
            "notify-suppression": self.suppression_fn,
        }

        for name, value in {
            "UploadBucketName": self.upload_bucket.bucket_name,
            "ComplianceArchiveBucketName": self.archive_bucket.bucket_name,
            "ArchiveWrappingKeyArn": self.wrapping_key.key_arn,
            "UploadBucketFunctionArn": self.upload_bucket_fn.function_arn,
            "ComplianceArchiveFunctionArn": self.archive_fn.function_arn,
            "UserPoolId": self.user_pool.user_pool_id,
            "ProfileTableName": self.profile_table.table_name,
            "BillingClusterArn": self.billing_cluster.cluster_arn,
            "BillingSecretArn": self.billing_secret_arn,
            "VectorBucketName": self.vector_bucket.vector_bucket_name or "",
            "VectorIndexName": self.vector_index_name,
            "AnalyticsDatabaseName": self.analytics_database_name,
            "AnalyticsTableName": self.analytics_table_name,
            "AnalyticsBucketName": self.analytics_bucket.bucket_name,
            "AthenaWorkgroupName": self.athena_workgroup.name,
            "ContactListName": self.contact_list_name,
        }.items():
            CfnOutput(self, name, value=value)

    # ── cognito-identity ─────────────────────────────────────────────────────────────

    def _build_cognito_identity(self, stage: str, idempotency: dynamodb.ITable) -> None:
        self.user_pool = cognito.UserPool(
            self,
            "SubjectPool",
            user_pool_name=f"asdp-{stage}-subjects",
            self_sign_up_enabled=False,
            removal_policy=RemovalPolicy.DESTROY if stage != "prod" else RemovalPolicy.RETAIN,
        )
        self.cognito_fn = self._function(
            "CognitoIdentityParticipant",
            stage=stage,
            system_id="cognito-identity",
            handler="pii_erasure.participants.cognito_identity.handler.lambda_handler",
            environment={
                "USER_POOL_ID": self.user_pool.user_pool_id,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        self.cognito_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminDeleteUser",
                    "cognito-idp:AdminDisableUser",
                    "cognito-idp:AdminEnableUser",
                    "cognito-idp:AdminUserGlobalSignOut",
                ],
                resources=[self.user_pool.user_pool_arn],
            )
        )
        idempotency.grant_read_write_data(self.cognito_fn)

    # ── profile-store ────────────────────────────────────────────────────────────────

    def _build_profile_store(
        self, stage: str, idempotency: dynamodb.ITable, *, keep: RemovalPolicy
    ) -> None:
        self.profile_table = dynamodb.Table(
            self,
            "ProfileStore",
            table_name=f"asdp-{stage}-profiles",
            partition_key=dynamodb.Attribute(
                name="subject_ref", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="item_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=keep,
        )
        # A real profile store has one, and the handler deliberately does not read it:
        # a GSI cannot be read consistently, so it is where a deleted subject keeps
        # appearing to exist. Present so that "we chose not to use it" is demonstrable.
        self.profile_table.add_global_secondary_index(
            index_name="by-tenant",
            partition_key=dynamodb.Attribute(name="tenant", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )
        self.profile_fn = self._function(
            "ProfileStoreParticipant",
            stage=stage,
            system_id="profile-store",
            handler="pii_erasure.participants.profile_store.handler.lambda_handler",
            environment={
                "PROFILE_TABLE": self.profile_table.table_name,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        self.profile_table.grant_read_write_data(self.profile_fn)
        idempotency.grant_read_write_data(self.profile_fn)

    # ── billing-ledger ───────────────────────────────────────────────────────────────

    def _build_billing_ledger(
        self, stage: str, idempotency: dynamodb.ITable, *, keep: RemovalPolicy
    ) -> None:
        # Aurora requires a VPC. There is no VPC-less Aurora — a cluster needs a DB subnet
        # group, and that needs subnets. The repo said "no VPC" in seven places; the
        # accurate claim is that **nothing we run attaches to one** (ADR-023, V8-4).
        #
        # So this VPC exists to hold the cluster and nothing else: isolated subnets only,
        # `nat_gateways=0`, no internet gateway, no VPC endpoints. Every one of those costs
        # money for existing, which the platform forbids; a VPC with none of them is free.
        # No Lambda joins it — the Data API is a public SigV4 endpoint — so the synth
        # assertion that no function carries a `VpcConfig` still holds and still matters.
        self.billing_vpc = ec2.Vpc(
            self,
            "BillingVpc",
            max_azs=2,  # Aurora requires subnets in at least two availability zones
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )

        # Serverless v2 at 0 ACU: no compute bill while idle, at the cost of cold-resume
        # latency. Storage still bills, which is why `make destroy-dev` still matters.
        self.billing_cluster = rds.DatabaseCluster(
            self,
            "BillingLedger",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_6
            ),
            vpc=self.billing_vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            serverless_v2_min_capacity=0,
            serverless_v2_max_capacity=2,
            serverless_v2_auto_pause_duration=Duration.minutes(5),
            enable_data_api=True,  # the reason no Lambda here needs a VPC
            default_database_name="billing",
            # Surfaced by cdk synth's own validator, not by review. A ledger holding
            # fabricated-but-treated-as-real financial PII is exactly the store that should
            # be encrypted at rest, and the default is off.
            storage_encrypted=True,
            removal_policy=keep,
        )
        secret = self.billing_cluster.secret
        assert secret is not None, "the cluster must manage its own credentials secret"
        self.billing_secret_arn = secret.secret_arn

        self.billing_fn = self._function(
            "BillingLedgerParticipant",
            stage=stage,
            system_id="billing-ledger",
            handler="pii_erasure.participants.billing_ledger.handler.lambda_handler",
            environment={
                "DB_CLUSTER_ARN": self.billing_cluster.cluster_arn,
                "DB_SECRET_ARN": self.billing_secret_arn,
                "DB_NAME": "billing",
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        self.billing_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                resources=[self.billing_cluster.cluster_arn],
            )
        )
        secret.grant_read(self.billing_fn)
        idempotency.grant_read_write_data(self.billing_fn)

    # ── vector-index ─────────────────────────────────────────────────────────────────

    def _build_vector_index(
        self, stage: str, idempotency: dynamodb.ITable, *, keep: RemovalPolicy
    ) -> None:
        bucket_name = f"asdp-{stage}-vectors-{self.account}"
        self.vector_index_name = "subject-embeddings"
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket", vector_bucket_name=bucket_name
        )
        self.vector_bucket.apply_removal_policy(keep)
        self.vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=self.vector_index_name,
            vector_bucket_name=bucket_name,
            data_type="float32",
            dimension=VECTOR_DIMENSION,
            distance_metric="cosine",
        )
        self.vector_index.add_dependency(self.vector_bucket)
        self.vector_index.apply_removal_policy(keep)

        index_arn = self.format_arn(
            service="s3vectors",
            resource="bucket",
            resource_name=f"{bucket_name}/index/{self.vector_index_name}",
        )
        self.vector_fn = self._function(
            "VectorIndexParticipant",
            stage=stage,
            system_id="vector-index",
            handler="pii_erasure.participants.vector_index.handler.lambda_handler",
            environment={
                "VECTOR_BUCKET_NAME": bucket_name,
                "VECTOR_INDEX_NAME": self.vector_index_name,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        self.vector_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:GetVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                resources=[index_arn],
            )
        )
        idempotency.grant_read_write_data(self.vector_fn)

    # ── analytics-lake ───────────────────────────────────────────────────────────────

    def _build_analytics_lake(
        self, stage: str, idempotency: dynamodb.ITable, *, keep: RemovalPolicy, prod: bool
    ) -> None:
        self.analytics_database_name = f"asdp_{stage}_lake".replace("-", "_")
        self.analytics_table_name = "events"

        self.analytics_bucket = s3.Bucket(
            self,
            "AnalyticsLake",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=keep,
            auto_delete_objects=not prod,
        )
        self.athena_results = s3.Bucket(
            self,
            "AthenaResults",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=keep,
            auto_delete_objects=not prod,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(1))],
        )
        self.analytics_glue_db = glue.CfnDatabase(
            self,
            "AnalyticsDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=self.analytics_database_name,
                description="ASDP analytics lake (Iceberg)",
            ),
        )
        self.athena_workgroup = athena.CfnWorkGroup(
            self,
            "AnalyticsWorkgroup",
            name=f"asdp-{stage}-analytics",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{self.athena_results.bucket_name}/results/"
                ),
                enforce_work_group_configuration=True,
            ),
        )
        self.athena_workgroup.apply_removal_policy(RemovalPolicy.DESTROY)

        self.analytics_fn = self._function(
            "AnalyticsLakeParticipant",
            stage=stage,
            system_id="analytics-lake",
            handler="pii_erasure.participants.analytics_lake.handler.lambda_handler",
            environment={
                "ATHENA_DATABASE": self.analytics_database_name,
                "ATHENA_TABLE": self.analytics_table_name,
                "ATHENA_WORKGROUP": self.athena_workgroup.name,
                "ATHENA_OUTPUT_LOCATION": (f"s3://{self.athena_results.bucket_name}/results/"),
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
            timeout=Duration.seconds(120),  # Athena is start-poll-read, not a single call
        )
        self.analytics_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                ],
                resources=[
                    self.format_arn(
                        service="athena",
                        resource="workgroup",
                        resource_name=self.athena_workgroup.name,
                    )
                ],
            )
        )
        self.analytics_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetDatabase",
                    "glue:GetTable",
                    "glue:UpdateTable",
                    "glue:GetPartitions",
                ],
                resources=[
                    self.format_arn(service="glue", resource="catalog", resource_name=None),
                    self.format_arn(
                        service="glue",
                        resource="database",
                        resource_name=self.analytics_database_name,
                    ),
                    self.format_arn(
                        service="glue",
                        resource="table",
                        resource_name=f"{self.analytics_database_name}/*",
                    ),
                ],
            )
        )
        self.analytics_bucket.grant_read_write(self.analytics_fn)
        self.athena_results.grant_read_write(self.analytics_fn)
        idempotency.grant_read_write_data(self.analytics_fn)

    # ── notify-suppression ───────────────────────────────────────────────────────────

    def _build_notify_suppression(self, stage: str, idempotency: dynamodb.ITable) -> None:
        self.contact_list_name = f"asdp-{stage}-meridian"
        self.contact_list = ses.CfnContactList(
            self,
            "ContactList",
            contact_list_name=self.contact_list_name,
            description="ASDP marketing contacts for the Meridian tenant",
        )
        self.contact_list.apply_removal_policy(RemovalPolicy.DESTROY)

        self.suppression_fn = self._function(
            "NotifySuppressionParticipant",
            stage=stage,
            system_id="notify-suppression",
            handler="pii_erasure.participants.notify_suppression.handler.lambda_handler",
            environment={
                "CONTACT_LIST_NAME": self.contact_list_name,
                "IDEMPOTENCY_TABLE": idempotency.table_name,
            },
        )
        self.suppression_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ses:GetContact",
                    "ses:CreateContact",
                    "ses:UpdateContact",
                    "ses:DeleteContact",
                ],
                resources=[
                    self.format_arn(
                        service="ses",
                        resource="contact-list",
                        resource_name=self.contact_list_name,
                    )
                ],
            )
        )
        # Read and write suppression, but never delete it. Invariant 7 as an IAM denial
        # rather than a promise: `ses:DeleteSuppressedDestination` is absent, so a handler
        # edit that tried to remove the entry would fail with AccessDenied instead of
        # silently undoing the subject's opt-out. Suppression is account-scoped, so it
        # cannot be narrowed below `*`.
        self.suppression_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:GetSuppressedDestination", "ses:PutSuppressedDestination"],
                resources=["*"],
            )
        )
        idempotency.grant_read_write_data(self.suppression_fn)

    def _function(
        self,
        construct_id: str,
        *,
        stage: str,
        system_id: str,
        handler: str,
        environment: dict[str, str],
        timeout: Duration | None = None,
    ) -> lambda_.Function:
        """One participant function. No VPC, ever — asserted at synth time."""
        return lambda_.Function(
            self,
            construct_id,
            function_name=f"asdp-{stage}-{system_id}",
            runtime=_RUNTIME,
            handler=handler,
            code=lambda_.Code.from_asset(LAMBDA_ASSET),
            timeout=timeout or Duration.seconds(60),
            memory_size=512,
            environment={"PII_ERASURE_STAGE": stage, **environment},
            description=f"ASDP participant: {system_id}",
        )
