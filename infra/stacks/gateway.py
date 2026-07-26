"""AgentCore Gateway: the single MCP endpoint in front of the participant Lambdas.

The Gateway converts each Lambda's declared tool schema into MCP tools, so the reasoning
plane speaks one protocol to one endpoint instead of learning eight SDKs.

**Inbound auth is `AWS_IAM`.** The callers at this stage are IAM principals — the saga
executor's role, and the conformance suite's. `CUSTOM_JWT` exists and is what an
end-user-facing gateway would use; adopting it here would mean standing up a Cognito user
pool for machine-to-machine calls that already have SigV4, and Cognito arrives in this
system as *participant #1* (M4) and as the operator API's authorizer (M8). Keeping those
apart is deliberate.

**Cedar is not attached yet.** `CfnGateway` takes a `policy_engine_configuration`, and
wiring it is M6's deliverable together with the `.cedar` files and the deploy-time schema
validation ADR-018 requires. An empty policy engine attached early would look like a
control and enforce nothing.

Every shape here — `AuthorizerType`, the MCP target configuration, `ToolDefinition`,
`GATEWAY_IAM_ROLE` — was read from the installed `bedrock-agentcore-control` API model and
the AgentCore developer guide rather than recalled (ROADMAP rule 3).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

_STRING = {"type": "string"}


def _artifact_array() -> dict[str, Any]:
    return {
        "type": "array",
        "description": "Echo of the approved artifact set",
        "items": {
            "type": "object",
            "properties": {
                "kind": _STRING,
                "locator": _STRING,
                "count": {"type": "integer"},
                "classification": {"type": "array", "items": _STRING},
                "retentionUntil": _STRING,
            },
            "required": ["kind", "locator"],
        },
    }


def _mutation_schema(*, approval: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "subjectRef": {"type": "string", "description": "Pseudonymous handle, never raw PII"},
        "sagaId": _STRING,
        "manifestDigest": {"type": "string", "description": "Binds this call to an approved plan"},
        "idempotencyKey": _STRING,
        "artifacts": _artifact_array(),
        "dryRun": {"type": "boolean"},
    }
    required = ["subjectRef", "sagaId", "manifestDigest", "idempotencyKey", "artifacts"]
    if approval:
        properties["approvalToken"] = {
            "type": "string",
            "description": "Digest-bound approval token (ADR-006)",
        }
        required.append("approvalToken")
    return {"type": "object", "properties": properties, "required": required}


def _read_schema(*, hints: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {"subjectRef": _STRING, "sagaId": _STRING}
    if hints:
        properties["scopeHints"] = {"type": "array", "items": _STRING}
    return {"type": "object", "properties": properties, "required": ["subjectRef", "sagaId"]}


#: The five verbs, as the Gateway will publish them. Order is fixed so the synthesised
#: template is stable; the read-only pair is first because that is the subset a discovery
#: identity is ever permitted to see (invariant 1, enforced by Cedar at M6).
TOOL_DEFINITIONS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("discover", "Read-only. What exists for this subject here?", _read_schema(hints=True)),
    ("verify", "Read-only assertion. Must return zero artifacts.", _read_schema(hints=False)),
    (
        "soft_delete",
        "Reversible. Disable, tombstone, or mark pending-anonymization.",
        _mutation_schema(approval=False),
    ),
    (
        "restore",
        "The compensating transaction for soft_delete. Never reachable from phase 3.",
        _mutation_schema(approval=False),
    ),
    (
        "hard_delete",
        "Irreversible. Purge or crypto-shred. Requires a digest-bound approval token.",
        _mutation_schema(approval=True),
    ),
)


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
        )

        self.targets: dict[str, agentcore.CfnGatewayTarget] = {}
        for system_id, function in participants.items():
            self.targets[system_id] = self._target(system_id, function)

        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier)
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url)
        CfnOutput(self, "GatewayRoleArn", value=self.role.role_arn)

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
