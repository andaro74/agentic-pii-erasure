"""AgentCore Gateway: the single MCP endpoint in front of the participant Lambdas.

The Gateway converts each Lambda's declared tool schema into MCP tools, so the reasoning
plane speaks one protocol to one endpoint instead of learning eight SDKs.

**Inbound auth is `AWS_IAM`.** The callers at this stage are IAM principals — the saga
executor's role, and the conformance suite's. `CUSTOM_JWT` exists and is what an
end-user-facing gateway would use; adopting it here would mean standing up a Cognito user
pool for machine-to-machine calls that already have SigV4, and Cognito arrives in this
system as *participant #1* (M4) and as the operator API's authorizer (M8). Keeping those
apart is deliberate.

**Cedar is attached here** (M6). A `CfnPolicyEngine` holds one `CfnPolicy` per file in
`policies/cedar/`, and the Gateway references it through `policy_engine_configuration`.
Two properties carry the weight:

* `validation_mode = FAIL_ON_ANY_FINDINGS` — AWS validates each policy against the
  schema **it** generated from this Gateway's tool manifest and refuses one that does
  not fit. That is ADR-018's requirement, enforced by the service rather than by us;
  `make policy-test` runs the same check hermetically against a reconstruction.
* `mode` is a **CloudFormation parameter**, not an environment variable, so
  `LOG_ONLY → ENFORCE` is a deploy and therefore lands in CloudTrail (§9.4).

What Cedar can express against the real schema — identity and request shape, not hold
counts or digest binding — is recorded in ADR-024.

Every shape here — `AuthorizerType`, the MCP target configuration, `ToolDefinition`,
`GATEWAY_IAM_ROLE` — was read from the installed `bedrock-agentcore-control` API model and
the AgentCore developer guide rather than recalled (ROADMAP rule 3).
"""

from __future__ import annotations

from aws_cdk import ArnFormat, CfnOutput, CfnParameter, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from pii_erasure.contract.tools import TOOL_DEFINITIONS
from pii_erasure.policy.engine import policy_files
from stacks.naming import agentcore_identifier


class GatewayStack(Stack):
    """One Gateway, one target per participant."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        participants: dict[str, lambda_.IFunction],
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        # ── LOG_ONLY → ENFORCE is a DEPLOY, not a runtime flag (§9.4) ────────
        # A CfnParameter rather than an env var or a context value on purpose:
        # flipping enforcement changes the template, so the change is a
        # CloudFormation event with an identity attached to it. Rollout order is
        # the default: observe the deny set against known-good trajectories
        # first, flip second. Skipping that produces an outage on day one and a
        # team that disables policy to restore service.
        self.policy_mode = CfnParameter(
            self,
            "PolicyEnforcementMode",
            type="String",
            default="LOG_ONLY",
            allowed_values=["LOG_ONLY", "ENFORCE"],
            description=(
                "AgentCore Policy mode. LOG_ONLY evaluates and records; ENFORCE denies. "
                "Deploy LOG_ONLY first and flip only when the deny set is empty (§9.4)."
            ),
        )

        # The Gateway assumes this role to invoke targets. Scoped to exactly the
        # participant functions — a wildcard here would let a future misconfigured
        # target reach any Lambda in the account.
        self.role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="AgentCore Gateway to participant Lambda invocation",
        )
        for function in participants.values():
            function.grant_invoke(self.role)

        # ── The Cedar runtime (ADR-018) ─────────────────────────────────────
        # `agentcore_identifier`, not the `asdp-{stage}-…` form used everywhere else:
        # Policy and PolicyEngine names take underscores only. See stacks/naming.py.
        self.policy_engine = agentcore.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name=agentcore_identifier("asdp", stage, "policy_engine"),
            description="ASDP Cedar policy set for the deletion Gateway",
        )

        # ── The Gateway's own permission to consult the engine (V10-4) ───────
        # Attaching an engine makes the Gateway a caller: it needs GetPolicyEngine
        # to read the engine's configuration and AuthorizeAction /
        # PartiallyAuthorizeActions to evaluate policies and filter tools/list.
        # The service verifies the first at ATTACH time, so this grant must exist
        # before the Gateway does — which is why the gateway resource below is a
        # name-scoped ARN pattern rather than the exact ARN: the exact ARN would
        # make the grant depend on the Gateway that cannot attach without it.
        # All three actions are granted together deliberately. The docs warn that
        # a missing GetPolicyEngine in LOG_ONLY mode fails SILENTLY and surfaces
        # only on the flip to enforcement — a decorative control, the exact
        # failure mode ADR-018 exists to rule out.
        gateway_arn_pattern = self.format_arn(
            service="bedrock-agentcore",
            resource="gateway",
            resource_name=f"asdp-{stage}-gateway-*",
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
        )
        self.policy_engine_access = iam.Policy(
            self,
            "PolicyEngineAccess",
            statements=[
                iam.PolicyStatement(
                    sid="PolicyEngineConfiguration",
                    actions=["bedrock-agentcore:GetPolicyEngine"],
                    resources=[self.policy_engine.attr_policy_engine_arn],
                ),
                iam.PolicyStatement(
                    sid="PolicyEngineAuthorization",
                    actions=[
                        "bedrock-agentcore:AuthorizeAction",
                        "bedrock-agentcore:PartiallyAuthorizeActions",
                    ],
                    # Both actions need BOTH resources — the docs are explicit
                    # that omitting either produces "policy engine not found".
                    resources=[
                        self.policy_engine.attr_policy_engine_arn,
                        gateway_arn_pattern,
                    ],
                ),
            ],
        )
        self.policy_engine_access.attach_to_role(self.role)

        self.gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name=f"asdp-{stage}-gateway",
            role_arn=self.role.role_arn,
            authorizer_type="AWS_IAM",
            protocol_type="MCP",
            description="ASDP deletion participants: one MCP endpoint, N backends",
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    instructions=(
                        "Erasure participants exposing the five-verb Deletion Participant "
                        "Contract. discover and verify are read-only."
                    ),
                )
            ),
            policy_engine_configuration=(
                agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
                    arn=self.policy_engine.attr_policy_engine_arn,
                    mode=self.policy_mode.value_as_string,
                )
            ),
        )
        # The grant above must be in effect when CloudFormation attaches the
        # engine; CFN cannot infer that from the template, so the dependency is
        # explicit. Without it the attach races IAM propagation (V10-4).
        self.gateway.node.add_dependency(self.policy_engine_access)

        # ── Ordering: gateway → targets → policies, and it cannot be otherwise ──
        # M6's first cut created the policies before the Gateway, on the assumption
        # that what the Gateway references must exist first. The service refused it
        # (V10-3): a tool-specific action list must be scoped to a SPECIFIC gateway —
        # `resource == AgentCore::Gateway::"<arn>"` — and the ARN only exists once the
        # Gateway does. The targets must exist too, because FAIL_ON_ANY_FINDINGS
        # validates each policy against the tool schema the targets declare. So the
        # policies come last, referencing the ARN, depending on every target. The
        # window in which the Gateway is live with no policies attached is fail-closed:
        # an empty Cedar set default-denies (and this stack deploys LOG_ONLY first
        # regardless, per §9.4).

        # ── The discovery identity the policies name ─────────────────────────
        # Created here, with the policy set, because a Cedar principal that names a
        # role which does not exist is a permit that can never match — and "the
        # policy is fine, the role is missing" is indistinguishable from "the policy
        # is wrong" when you are staring at a deny. The Runtime that assumes it lands
        # at M7; the role carries no participant IAM and never will (§9.3), because
        # its only route to the participants is through this Gateway.
        self.discovery_role = iam.Role(
            self,
            "DiscoveryRole",
            role_name=f"asdp-{stage}-discovery",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="ASDP discovery identity: read-only at the Gateway (invariant 1)",
        )
        self.discovery_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[self.gateway.attr_gateway_arn],
            )
        )

        self.targets: dict[str, agentcore.CfnGatewayTarget] = {}
        for system_id, function in participants.items():
            self.targets[system_id] = self._target(system_id, function)

        self.policies = self._attach_policies(stage)
        for policy in self.policies:
            # Referencing the Gateway ARN already orders each policy after the Gateway;
            # the targets are the part CloudFormation cannot infer. Without them a
            # policy can be validated against a tool schema that does not mention its
            # actions yet, and FAIL_ON_ANY_FINDINGS makes that a deploy failure.
            for target in self.targets.values():
                policy.node.add_dependency(target)

        CfnOutput(self, "PolicyEngineArn", value=self.policy_engine.attr_policy_engine_arn)
        CfnOutput(self, "DiscoveryRoleArn", value=self.discovery_role.role_arn)
        CfnOutput(self, "PolicyMode", value=self.policy_mode.value_as_string)
        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url)
        CfnOutput(self, "GatewayRoleArn", value=self.role.role_arn)

    def _attach_policies(self, stage: str) -> list[agentcore.CfnPolicy]:
        """One `CfnPolicy` per `.cedar` file — the file boundary IS the policy boundary.

        `validation_mode=FAIL_ON_ANY_FINDINGS` is the control ADR-018 asks for: AWS
        validates each statement against the schema it generated from THIS Gateway's
        tool manifest, and the deploy fails if a policy references something the
        manifest does not declare. Without it, a policy naming a context key the
        Gateway never injects would deploy clean and silently never fire.

        `enforcement_mode` stays ACTIVE on every policy; the LOG_ONLY/ENFORCE switch
        lives once, on the Gateway, so there is exactly one thing to flip and one
        thing to read in CloudTrail.
        """
        policies: list[agentcore.CfnPolicy] = []
        for path in policy_files():
            # `attr_gateway_arn` is a CloudFormation token, so the rendered statement
            # becomes an Fn::Join resolving at deploy — each policy names the one
            # gateway it governs, which the service requires for tool-specific
            # policies (V10-3) and which also orders policy-after-gateway for free.
            statement = (
                path.read_text(encoding="utf-8")
                .replace("{stage}", stage)
                .replace("{gateway_arn}", self.gateway.attr_gateway_arn)
            )
            construct_id = "Policy" + "".join(
                part.capitalize() for part in path.stem.split("-") if not part.isdigit()
            )
            policies.append(
                agentcore.CfnPolicy(
                    self,
                    construct_id,
                    name=agentcore_identifier("asdp", stage, path.stem),
                    policy_engine_id=self.policy_engine.attr_policy_engine_id,
                    description=f"ASDP Cedar policy: {path.stem}",
                    enforcement_mode="ACTIVE",
                    validation_mode="FAIL_ON_ANY_FINDINGS",
                    definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                        cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=statement)
                    ),
                )
            )
        return policies

    def _target(self, system_id: str, function: lambda_.IFunction) -> agentcore.CfnGatewayTarget:
        """Register one participant Lambda as an MCP target.

        The published tool names are target-prefixed by the service —
        ``upload-bucket___discover`` — and the handler strips the prefix at the
        ``___`` delimiter. That prefixing is also why the tool surface grows with
        participant count rather than staying flat; see the note in ARCHITECTURE §4.
        """
        construct_id = "".join(part.capitalize() for part in system_id.split("-")) + "Target"
        return agentcore.CfnGatewayTarget(
            self,
            construct_id,
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=system_id,
            description=f"ASDP participant: {system_id}",
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=function.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[
                                agentcore.CfnGatewayTarget.ToolDefinitionProperty(
                                    name=name,
                                    description=description,
                                    input_schema=schema,
                                )
                                for name, description, schema in TOOL_DEFINITIONS
                            ]
                        ),
                    )
                )
            ),
        )
