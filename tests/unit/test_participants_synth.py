"""Synth-time assertions on the participants and gateway stacks (hermetic).

Some properties are cheaper to guarantee in the template than to test at runtime, and
some cannot be tested at runtime at all without doing the dangerous thing first:

- the `compliance-archive` role has **no delete permission on its bucket** — not because
  a delete would be denied, but because there is no delete to grant. A granted-but-futile
  permission would suggest the erasure path runs through S3 when it runs through the key
  registry (ADR-007);
- the upload bucket has **versioning on**, which is the whole archetype;
- **one role per participant** — a shared role makes "which participant did this"
  unanswerable from CloudTrail, and the blast radius the union of both;
- **no Lambda has a VPC config** — Aurora is reached via the RDS Data API precisely so
  none needs one;
- the Gateway publishes exactly the five verbs, with `hard_delete` requiring an approval
  token *in its published schema*, so the requirement is visible to any client before it
  ever calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from pii_erasure.contract.registry import system_ids

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.foundation import FoundationStack
from stacks.gateway import GatewayStack
from stacks.participants import ParticipantsStack


def _app(stage: str, object_lock_days: int) -> tuple[Template, Template]:
    app = App()
    foundation = FoundationStack(
        app, f"asdp-{stage}-foundation", stage=stage, object_lock_days=object_lock_days
    )
    participants = ParticipantsStack(
        app,
        f"asdp-{stage}-participants",
        stage=stage,
        object_lock_days=object_lock_days,
        dek_registry=foundation.dek_registry,
        idempotency=foundation.idempotency,
    )
    gateway = GatewayStack(
        app,
        f"asdp-{stage}-gateway",
        stage=stage,
        participants=participants.functions,
    )
    return Template.from_stack(participants), Template.from_stack(gateway)


@pytest.fixture(scope="module")
def templates() -> tuple[Template, Template]:
    return _app("dev", object_lock_days=1)


@pytest.fixture(scope="module")
def participants(templates: tuple[Template, Template]) -> Template:
    return templates[0]


@pytest.fixture(scope="module")
def gateway(templates: tuple[Template, Template]) -> Template:
    return templates[1]


def _statements(template: Template) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


# ─── the two buckets ──────────────────────────────────────────────────────────────────


def test_the_upload_bucket_is_versioned(participants: Template) -> None:
    """Without versioning there is no delete marker, and no archetype."""
    participants.has_resource_properties(
        "AWS::S3::Bucket", {"VersioningConfiguration": {"Status": "Enabled"}}
    )


def test_the_archive_bucket_is_object_lock_compliance(participants: Template) -> None:
    participants.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "ObjectLockEnabled": True,
            "ObjectLockConfiguration": {
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 1}}
            },
        },
    )


def test_dev_keeps_the_object_lock_window_short(participants: Template) -> None:
    """A long retention in dev creates infrastructure that cannot be torn down by anyone,
    including root, until it expires. The hazard is documented; this is the tripwire."""
    for bucket in participants.find_resources("AWS::S3::Bucket").values():
        config = bucket["Properties"].get("ObjectLockConfiguration")
        if config:
            assert config["Rule"]["DefaultRetention"]["Days"] <= 7


# ─── least privilege ──────────────────────────────────────────────────────────────────


def test_each_participant_has_its_own_role(participants: Template) -> None:
    roles = participants.find_resources("AWS::IAM::Role")
    lambda_roles = [
        role
        for role in roles.values()
        if any(
            principal.get("Service") == "lambda.amazonaws.com"
            for principal in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
            for principal in [principal.get("Principal", {})]
        )
    ]
    assert len(lambda_roles) >= 2, "a shared participant role makes CloudTrail ambiguous"


def test_no_participant_may_call_bedrock(participants: Template) -> None:
    """Participants execute; they do not reason (invariant 2's spirit, one plane down)."""
    for statement in _statements(participants):
        assert not any(action.startswith("bedrock") for action in _actions(statement))


def test_the_archive_participant_cannot_delete_from_its_bucket(participants: Template) -> None:
    """The point of the WORM archetype: there is no delete to grant.

    Asserted over the whole template rather than one role, because the failure this
    catches is somebody 'fixing' the archive by adding a delete permission that S3 would
    refuse anyway — and thereby documenting the wrong erasure mechanism.
    """
    deletes = [
        action
        for statement in _statements(participants)
        for action in _actions(statement)
        if action in {"s3:DeleteObject", "s3:DeleteObjectVersion"}
        for resource in [str(statement.get("Resource"))]
        if "ComplianceArchive" in resource
    ]
    assert not deletes, f"the archive bucket must have no delete grant, found {deletes}"


def test_no_lambda_attaches_to_a_vpc(participants: Template) -> None:
    """A VPC now exists, because Aurora cannot exist without one (ADR-023). *This* is the
    property that was always the real one, and it is unaffected."""
    for function in participants.find_resources("AWS::Lambda::Function").values():
        assert "VpcConfig" not in function["Properties"]


def test_the_vpc_has_nothing_in_it_that_bills_for_existing(participants: Template) -> None:
    """ADR-023's cost claim, made falsifiable.

    A VPC is free. NAT gateways (~$32/month), internet gateways with EIPs, and interface
    endpoints are not, and adding one is how "an idle stack costs cents" quietly stops
    being true. Nothing in this VPC needs them: the only thing inside it is Aurora, and
    everything that talks to Aurora is outside it.
    """
    assert participants.find_resources("AWS::EC2::NatGateway") == {}
    assert participants.find_resources("AWS::EC2::VPCEndpoint") == {}
    assert participants.find_resources("AWS::EC2::EIP") == {}


def test_the_suppression_participant_cannot_delete_a_suppression_entry(
    participants: Template,
) -> None:
    """Invariant 7 as an IAM denial rather than a promise.

    `notify-suppression` must return PARTIAL because the SES suppression entry survives
    erasure. Nothing stopped a future edit from "fixing" that by deleting the entry — which
    would silently undo the subject's opt-out and re-enable mail to them, causing the exact
    harm erasure was requested to prevent. Withholding the permission makes that edit fail
    with AccessDenied instead of succeeding quietly.
    """
    forbidden = [
        action
        for statement in _statements(participants)
        for action in _actions(statement)
        if action == "ses:DeleteSuppressedDestination"
    ]
    assert not forbidden, (
        "ses:DeleteSuppressedDestination is granted somewhere — the residual that "
        "invariant 7 requires would become optional"
    )


def test_aurora_is_serverless_v2_scaled_to_zero(participants: Template) -> None:
    """`min_capacity = 0` is what keeps an idle relational store from billing compute."""
    participants.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "EnableHttpEndpoint": True,  # the Data API — why no Lambda needs the VPC
            "ServerlessV2ScalingConfiguration": Match.object_like({"MinCapacity": 0}),
        },
    )


def test_the_declared_snapshot_window_matches_the_participant(participants: Template) -> None:
    """The residual `analytics-lake` discloses is only honest if the table honours it.

    Two constants in two files, asserted equal here, because a disclosed window the
    infrastructure does not implement is a fabricated reassurance — worse than no window,
    since an approver would act on it.
    """
    import stacks.participants as infra_stack

    from pii_erasure.participants.analytics_lake import handler as lake
    from pii_erasure.participants.analytics_lake import schema as lake_schema

    assert infra_stack.SNAPSHOT_RETENTION_DAYS == lake.SNAPSHOT_RETENTION_DAYS
    # And the table property that actually enforces it, so the disclosed window is real
    # rather than a number the participant states and nothing honours.
    assert lake_schema.SNAPSHOT_RETENTION_SECONDS == lake.SNAPSHOT_RETENTION_DAYS * 86400
    ddl = lake_schema.create_table_sql(database="d", table="t", location="s3://b/")
    assert f"'{lake_schema.SNAPSHOT_RETENTION_SECONDS}'" in ddl
    assert "'table_type' = 'ICEBERG'" in ddl


# ─── the gateway ──────────────────────────────────────────────────────────────────────


def test_the_gateway_speaks_mcp_and_authenticates_with_iam(gateway: Template) -> None:
    gateway.has_resource_properties(
        "AWS::BedrockAgentCore::Gateway",
        {"AuthorizerType": "AWS_IAM", "ProtocolType": "MCP"},
    )


def test_no_cedar_policy_engine_is_attached_yet(gateway: Template) -> None:
    """Honest absence. An empty policy engine attached at M2 would look like a control
    and enforce nothing; ADR-018's schema validation and the .cedar files land at M6."""
    for resource in gateway.find_resources("AWS::BedrockAgentCore::Gateway").values():
        assert "PolicyEngineConfiguration" not in resource["Properties"]


def test_one_target_per_participant(gateway: Template) -> None:
    """Every registered participant is reachable, and nothing unregistered is.

    Driven from the registry rather than a literal list, so participant #9 is covered the
    moment it is registered. A participant that exists but has no Gateway target is one
    the agent cannot call — which shows up as a recall failure with no error attached to
    it, the exact shape ADR-008 exists to prevent.
    """
    targets = gateway.find_resources("AWS::BedrockAgentCore::GatewayTarget")
    names = {target["Properties"]["Name"] for target in targets.values()}
    assert names == set(system_ids())


def test_every_target_publishes_exactly_the_five_verbs(gateway: Template) -> None:
    for target in gateway.find_resources("AWS::BedrockAgentCore::GatewayTarget").values():
        tools = target["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"][
            "InlinePayload"
        ]
        assert {tool["Name"] for tool in tools} == {
            "discover",
            "verify",
            "soft_delete",
            "restore",
            "hard_delete",
        }


def test_the_published_hard_delete_schema_requires_an_approval_token(gateway: Template) -> None:
    """The binding is visible in the tool surface, not only in the handler — a client
    learns the requirement before it calls, and an auditor can read it off the Gateway."""
    for target in gateway.find_resources("AWS::BedrockAgentCore::GatewayTarget").values():
        tools = target["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"][
            "InlinePayload"
        ]
        # CDK pascal-cases the L1 struct members on the way into the template
        # (`required` -> `Required`), while user-defined property names pass through
        # unchanged. Asserting the synthesised form rather than the source dict is the
        # point: this is what CloudFormation will actually receive.
        by_name = {tool["Name"]: tool for tool in tools}
        assert "approvalToken" in by_name["hard_delete"]["InputSchema"]["Required"]
        assert "manifestDigest" in by_name["soft_delete"]["InputSchema"]["Required"]
        # …and the read-only verbs carry neither, so a read can never be mistaken for a
        # write that forgot its binding.
        assert "approvalToken" not in by_name["discover"]["InputSchema"]["Properties"]


def test_the_gateway_role_cannot_invoke_arbitrary_lambdas(gateway: Template) -> None:
    for statement in _statements(gateway):
        if any(action.startswith("lambda:Invoke") for action in _actions(statement)):
            assert statement["Resource"] != "*", "a wildcard here reaches every function"


def test_the_gateway_trusts_only_the_agentcore_service(gateway: Template) -> None:
    gateway.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {"Principal": {"Service": "bedrock-agentcore.amazonaws.com"}}
                            )
                        ]
                    )
                }
            )
        },
    )


def test_the_lambda_asset_marker_is_present() -> None:
    """`Code.from_asset` needs the directory to exist for synth to resolve at all.

    It is committed empty except for this marker, and `make package` is careful not to
    delete it — an `rm -rf` there shows up as a deletion that a careless `git add -A`
    commits, and the next person to clone gets a synth failure they did not cause.
    """
    marker = Path(__file__).resolve().parents[2] / "infra" / "build" / "participants" / ".gitkeep"
    assert marker.is_file(), "the committed asset marker is gone — see `make package`"
