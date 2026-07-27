"""AgentCore Runtime + Memory — the reasoning plane, and the least privileged one.


The critical inversion this stack encodes: **the only compute allowed to call a model
is the only compute with no permission to touch subject data.** The Runtime role below
carries Bedrock model invocation, its Memory namespace, and `InvokeGateway`. It carries
no DynamoDB, no S3, no Cognito, no Aurora, no KMS decrypt — no participant service at
all. Its single route to a participant is a Cedar-evaluated call through the Gateway,
where `PartiallyAuthorizeActions` filters the tool list so a mutating verb is never even
offered (invariant 1).

That absence is the milestone's hermetic gate, and it is asserted as an absence in
`tests/unit/test_runtime_synth.py` — a "no participant IAM" claim checked by listing what
the role *has* rather than by trusting what we meant to give it.

**Ships as an S3 code zip, not a container** (ADR-025).
`aws_s3_assets.Asset` needs no Docker, which keeps `cdk synth` — and therefore
`make check` — free of a daemon dependency. A `DockerImageAsset` would build at synth
time and put arm64 emulation inside the hermetic gate.

Every shape here was read from the installed `bedrock-agentcore-control` service
model
and the AgentCore developer guide rather than recalled (ROADMAP rule 3), and the
`Name` fields go through `agentcore_identifier` because Runtime and Memory names take
underscores only — V10-1's lesson, applied before a deploy rather than during one.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

from stacks.naming import agentcore_identifier

#: Where `make package` stages the arm64 dependency tree plus `entrypoint.py`.
RUNTIME_ASSET = Path(__file__).resolve().parents[1] / "build" / "runtime"

#: Must match `RUNTIME_ENTRYPOINT` in the Makefile and the file `make package` copies
#: to the zip root. A synth assertion pins the three together, because `entryPoint` is
#: a filename the control plane does not validate — a rename deploys clean and fails at
#: the first invocation (ADR-025, cost 3).
ENTRYPOINT_FILE = "entrypoint.py"

#: AgentCore Runtime is arm64-only and runs Python from the zip. Kept beside the
#: Makefile's RUNTIME_PY so a bump is one grep, not two discoveries.
RUNTIME_VERSION = "PYTHON_3_13"

#: Long enough that a topology prior survives a quiet fortnight; short enough that a
#: decommissioned system's prior decays rather than misleading discovery forever
#: (ADR-019, cost 2). Priors are advisory, so expiry costs ordering, never recall.
MEMORY_EVENT_EXPIRY_DAYS = 90

#: A discovery pass is eight Gateway round-trips and some model calls — minutes, not
#: hours. These bound a wedged run rather than describing a healthy one.
IDLE_SESSION_TIMEOUT_SECONDS = 300
MAX_SESSION_LIFETIME_SECONDS = 1800


class RuntimeStack(Stack):
    """The discovery Runtime, its Memory store, and a role that can reach neither
    a participant nor a checkpoint."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        gateway_arn: str,
        gateway_url: str,
        discovery_role: iam.IRole,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        # ── Topology priors (ADR-019, invariant 13) ──────────────────────────
        # No memory strategies are configured, and that is the decision, not an
        # omission: a strategy runs an extraction model over conversation events to
        # decide what is worth remembering — from a transcript containing artifact
        # locators and subject handles. Records are written explicitly by
        # `discovery/memory.py` through a scrubber that REJECTS, so nothing reaches
        # Memory that was not named by code a reviewer can read.
        self.memory = agentcore.CfnMemory(
            self,
            "TopologyMemory",
            name=agentcore_identifier("asdp", stage, "topology"),
            description="ASDP tenant topology priors. Topology only, never subject data.",
            event_expiry_duration=MEMORY_EVENT_EXPIRY_DAYS,
        )

        # ── The code zip ─────────────────────────────────────────────────────
        self.asset = s3_assets.Asset(self, "RuntimeCode", path=str(RUNTIME_ASSET))

        # ── ONE identity, ONE role (V10-6) ──────────────────────────────────
        # This is `asdp-{stage}-discovery`, created in the gateway stack alongside the
        # Cedar policy set that names it. M7's first cut created a SECOND role,
        # `asdp-{stage}-discovery-runtime`, and the Runtime assumed that one — so every
        # Gateway call default-denied, because Cedar's `like "*:assumed-role/
        # asdp-dev-discovery"` is an exact suffix match and does not match
        # `...-discovery-runtime`. Two roles for one identity is the bug; the fix is
        # one role, not a looser permit.
        self.role = discovery_role

        # Permissions live in a policy owned by THIS stack, not on the role's default
        # policy. Adding to a role from another stack would make the gateway stack
        # import the memory ARN and asset bucket while this stack imports the gateway
        # ARN — a reference cycle CloudFormation refuses.
        self.permissions = iam.Policy(
            self,
            "DiscoveryRuntimePermissions",
            roles=[discovery_role],
            statements=[
                # The ONLY Bedrock permission in the platform. The saga has none
                # (invariant 12), the participants have none. This one statement is
                # the whole reasoning-plane privilege.
                iam.PolicyStatement(
                    sid="InvokeFoundationModels",
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[
                        self.format_arn(
                            service="bedrock",
                            region=self.region,
                            account="",
                            resource="foundation-model",
                            resource_name="*",
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        ),
                        self.format_arn(
                            service="bedrock",
                            resource="inference-profile",
                            resource_name="*",
                            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                        ),
                    ],
                ),
                # The single route to subject data: through the Gateway, where
                # Cedar decides. Kept in THIS policy rather than on the role in the
                # gateway stack for two reasons — every permission the reasoning
                # plane holds is then readable in one place, and referencing the
                # Gateway ARN here keeps its cross-stack export in use. Dropping the
                # last reference to an export the deployed stack still imports is a
                # rollback CloudFormation cannot complete (V10-7).
                iam.PolicyStatement(
                    sid="InvokeTheGatewayAndNothingElse",
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=[gateway_arn, f"{gateway_arn}/*"],
                ),
                # Its own Memory namespace, scoped to this store: a second tenant's
                # priors — or the checkpointer — is unreachable from here.
                iam.PolicyStatement(
                    sid="TopologyPriors",
                    actions=[
                        "bedrock-agentcore:BatchCreateMemoryRecords",
                        "bedrock-agentcore:RetrieveMemoryRecords",
                        "bedrock-agentcore:ListMemoryRecords",
                    ],
                    resources=[self.memory.attr_memory_arn],
                ),
                # It reads its own deployment package, and nothing else.
                iam.PolicyStatement(
                    sid="ReadOwnDeploymentPackage",
                    actions=["s3:GetObject", "s3:GetObjectVersion"],
                    resources=[f"arn:aws:s3:::{self.asset.s3_bucket_name}/*"],
                ),
                # CreateLogGroup included deliberately (V10-6b): without it AgentCore
                # creates no log group at all, so a failing Runtime answers 500 and the
                # service's own advice — "check your CloudWatch logs" — points at
                # nothing. An undiagnosable failure is worse than a noisy one.
                iam.PolicyStatement(
                    sid="Observability",
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogStreams",
                        "logs:DescribeLogGroups",
                        "cloudwatch:PutMetricData",
                        "xray:PutTraceSegments",
                        "xray:PutTelemetryRecords",
                    ],
                    resources=["*"],
                ),
            ],
        )

        self.runtime = agentcore.CfnRuntime(
            self,
            "DiscoveryRuntime",
            agent_runtime_name=agentcore_identifier("asdp", stage, "discovery"),
            role_arn=self.role.role_arn,
            description="ASDP discovery subgraph. Read-only at the Gateway (invariant 1).",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(
                            bucket=self.asset.s3_bucket_name,
                            prefix=self.asset.s3_object_key,
                        )
                    ),
                    entry_point=[ENTRYPOINT_FILE],
                    runtime=RUNTIME_VERSION,
                )
            ),
            # PUBLIC, not VPC: nothing in this platform attaches to a VPC (ADR-023),
            # and the Runtime's egress targets — Bedrock, the Gateway, Memory — are
            # all public AWS endpoints. A VPC here would need NAT or endpoints, both
            # of which bill continuously for existing.
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC"
            ),
            # A bare string on the L1, not a property object: CloudFormation flattens
            # this one where the API nests it. Read off the installed construct rather
            # than mirrored from the service model, which is why it is not
            # `ProtocolConfigurationProperty(...)`.
            protocol_configuration="HTTP",
            # Bound the blast radius of a hung discovery run. AgentCore keeps a session
            # alive while /ping answers HealthyBusy, so a wedged agent could otherwise
            # hold a microVM until the service default expires — billed for working,
            # but working on nothing.
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=IDLE_SESSION_TIMEOUT_SECONDS,
                max_lifetime=MAX_SESSION_LIFETIME_SECONDS,
            ),
            environment_variables={
                "ASDP_GATEWAY_URL": gateway_url,
                "ASDP_MEMORY_ID": self.memory.attr_memory_id,
                "ASDP_STAGE": stage,
            },
        )

        CfnOutput(self, "RuntimeArn", value=self.runtime.attr_agent_runtime_arn)
        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id)
        CfnOutput(self, "MemoryArn", value=self.memory.attr_memory_arn)
        CfnOutput(self, "DiscoveryRuntimeRoleArn", value=self.role.role_arn)
