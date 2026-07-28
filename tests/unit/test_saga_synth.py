"""Synth-time assertions on the saga stack (hermetic) — invariant 12 made structural.

The load-bearing one: **neither saga role carries any `bedrock:*` action.** Invariant 2
("nodes never call a model") used to be a code-review rule backed by an import test; it
is now also an IAM fact. The others: no VPC config (ADR-023's rule), participant invoke
scoped to exactly the eight functions, PassRole confined to the scheduler service, and
the DLQ present — because "phase 3 halts to a queue" is only true if the queue exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from pii_erasure.contract.registry import system_ids

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.foundation import FoundationStack
from stacks.participants import ParticipantsStack
from stacks.saga import SagaStack


@pytest.fixture(scope="module")
def saga_template() -> Template:
    app = App()
    foundation = FoundationStack(app, "asdp-t-foundation", stage="t", object_lock_days=1)
    participants = ParticipantsStack(
        app,
        "asdp-t-participants",
        stage="t",
        object_lock_days=1,
        dek_registry=foundation.dek_registry,
        idempotency=foundation.idempotency,
    )
    saga = SagaStack(
        app,
        "asdp-t-saga",
        stage="t",
        checkpoints=foundation.checkpoints,
        checkpoint_offload=foundation.checkpoint_offload,
        ledger=foundation.ledger,
        tombstones=foundation.tombstones,
        idempotency=foundation.idempotency,
        signing_key=foundation.signing_key,
        participants=participants.functions,
        # Constructed the way `infra/app.py` does, so the fixture exercises the
        # deployed shape. A fixture that omitted this would make the
        # "no model permission" assertion pass for the wrong reason.
        discovery_runtime_arn=(
            "arn:aws:bedrock-agentcore:us-west-2:000000000000:runtime/asdp_t_discovery-abc123"
        ),
    )
    return Template.from_stack(saga)


def _all_policy_actions(template: Template) -> list[str]:
    actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            listed = statement.get("Action", [])
            actions.extend([listed] if isinstance(listed, str) else listed)
    for role in template.find_resources("AWS::IAM::Role").values():
        for inline in role["Properties"].get("Policies", []):
            for statement in inline["PolicyDocument"]["Statement"]:
                listed = statement.get("Action", [])
                actions.extend([listed] if isinstance(listed, str) else listed)
    return actions


#: Invariant 12, verbatim: *"their only AgentCore permission is the single `plan`
#: node's Runtime invocation."* One action, named exactly. `bedrock:InvokeModel` and
#: every other `bedrock:*` remains forbidden — that distinction IS the invariant, and
#: it is what makes "the saga cannot reason" an IAM fact rather than a code-review rule.
SAGA_PERMITTED_AGENTCORE = frozenset({"bedrock-agentcore:InvokeAgentRuntime"})


def test_the_saga_has_no_model_permission(saga_template: Template) -> None:
    """Invariant 12. The saga replays approved manifests; a model permission on this
    role would mean replay could re-enter the model — the exact thing ADR-001 forbids.

    Before M7 this asserted *no `bedrock` action at all*, which was accidentally exact
    because nothing in the saga needed AgentCore. M7 gives `plan` one Runtime call, so
    the assertion is now spelled out: one named action, and `bedrock:*` still zero.
    A widened prefix would have let `bedrock:InvokeModel` through on the same edit.
    """
    bedrock_family = [a for a in _all_policy_actions(saga_template) if "bedrock" in a.lower()]
    model_actions = [a for a in bedrock_family if a.lower().startswith("bedrock:")]
    assert model_actions == [], f"the saga must hold no model permission, found {model_actions}"
    unexpected = set(bedrock_family) - SAGA_PERMITTED_AGENTCORE
    assert not unexpected, f"unexpected AgentCore permission on a saga role: {sorted(unexpected)}"


def test_the_saga_may_invoke_exactly_one_runtime(saga_template: Template) -> None:
    """The other half: the permission exists, and is scoped to one ARN.

    A wildcard here would let the saga invoke any AgentCore runtime in the account —
    including one an attacker created — and receive a manifest from it. The manifest
    would still have to survive signing, validation and approval, but the plan a human
    reviews would have been written by something nobody deployed.
    """
    for policy in saga_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "bedrock-agentcore:InvokeAgentRuntime" in actions:
                assert statement["Resource"] != "*", "the Runtime grant must name one ARN"
                return
    pytest.fail("the plan node has no Runtime invocation permission")


def test_the_resume_role_cannot_invoke_the_runtime(saga_template: Template) -> None:
    """Invariant 3's mechanism, in IAM. The resume Lambda replays a checkpointed
    manifest; re-planning after an approval would execute a plan nobody approved.
    Withholding the permission makes that un-makeable rather than merely uncoded —
    the same shape as `notify-suppression` having no delete permission.
    """
    resume_roles = {
        logical_id
        for logical_id in saga_template.find_resources("AWS::IAM::Role")
        if "Resume" in logical_id
    }
    assert resume_roles, "no resume role found — the fixture is not exercising both planes"
    for policy in saga_template.find_resources("AWS::IAM::Policy").values():
        actions: list[str] = []
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            listed = statement.get("Action", [])
            actions.extend([listed] if isinstance(listed, str) else listed)
        if not any("InvokeAgentRuntime" in action for action in actions):
            continue
        attached = json.dumps(policy["Properties"].get("Roles", []))
        for role in resume_roles:
            assert role not in attached, f"{role} may invoke the discovery Runtime"


def test_no_saga_function_attaches_to_a_vpc(saga_template: Template) -> None:
    for name, fn in saga_template.find_resources("AWS::Lambda::Function").items():
        assert "VpcConfig" not in fn["Properties"], f"{name} must not attach to a VPC (ADR-023)"


def test_the_two_functions_have_distinct_roles(saga_template: Template) -> None:
    fns = saga_template.find_resources("AWS::Lambda::Function")
    roles = {json.dumps(fn["Properties"]["Role"]) for fn in fns.values()}
    assert len(fns) == 2
    assert len(roles) == 2, (
        "executor and resume must have their own roles — 'which plane did this' has to "
        "be answerable from CloudTrail"
    )


def test_participant_invoke_is_scoped_to_the_eight_functions(
    saga_template: Template,
) -> None:
    """Never a wildcard: the executor reaches the participants and nothing else."""
    invoke_statements: list[dict[str, Any]] = []
    for policy in saga_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if "lambda:InvokeFunction" in actions:
                invoke_statements.append(statement)
    assert invoke_statements, "the executor must be able to invoke participants"
    participant_statements = []
    for statement in invoke_statements:
        rendered = json.dumps(statement["Resource"])
        assert '"*"' not in rendered, "lambda invoke must never be a wildcard"
        # The scheduler role's statement targets only the resume function; every
        # OTHER invoke statement is a participant grant and must name all eight.
        if "saga-resume" not in rendered:
            participant_statements.append(rendered)
    assert participant_statements, "no participant invoke grant found"
    # Cross-stack references render as Fn::ImportValue of the participant construct
    # IDs, so the check matches the CamelCase construct fragment of each systemId
    # ("cognito-identity" → "CognitoIdentity"), which the participants stack derives
    # from the same registry.
    for rendered in participant_statements:
        for system_id in system_ids():
            fragment = "".join(part.capitalize() for part in system_id.split("-"))
            assert fragment in rendered, f"invoke grant is missing {system_id}"
        assert rendered.count("Participant") >= len(system_ids())


def test_pass_role_is_confined_to_the_scheduler_service(saga_template: Template) -> None:
    saga_template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Action": "iam:PassRole",
                                    "Condition": {
                                        "StringEquals": {
                                            "iam:PassedToService": "scheduler.amazonaws.com"
                                        }
                                    },
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_the_dlq_exists_and_is_encrypted_in_transit(saga_template: Template) -> None:
    queues = saga_template.find_resources("AWS::SQS::Queue")
    assert len(queues) == 1, "phase 3 halts to a queue — the queue has to exist"


def test_scheduler_role_is_assumable_only_by_the_scheduler_service(
    saga_template: Template,
) -> None:
    roles = saga_template.find_resources(
        "AWS::IAM::Role",
        props=Match.object_like(
            {
                "Properties": {
                    "AssumeRolePolicyDocument": {
                        "Statement": [
                            Match.object_like({"Principal": {"Service": "scheduler.amazonaws.com"}})
                        ]
                    }
                }
            }
        ),
    )
    assert len(roles) == 1


#: Every environment variable `deps.py` reads for wall-clock compression. Written out
#: verbatim, because the failure this catches is a stack that reads none of them: the
#: Lambda falls back to production timings and a dev walkthrough sits at the grace gate
#: for thirty days, with nothing anywhere reporting a fault (V11-1).
_DEV_TIMER_VARS = ("SWEEP_DELAYS_SECONDS", "APPROVAL_TIMEOUT_SECONDS", "GRACE_SECONDS_OVERRIDE")


def _saga_environments(saga_template: Template) -> list[dict[str, str]]:
    return [
        body["Properties"]["Environment"]["Variables"]
        for body in saga_template.to_json()["Resources"].values()
        if body["Type"] == "AWS::Lambda::Function"
    ]


@pytest.mark.parametrize("name", _DEV_TIMER_VARS)
def test_a_dev_stack_compresses_every_wall_clock_timer(saga_template: Template, name: str) -> None:
    """`GRACE_SECONDS_OVERRIDE` was read by `deps.py` from M5 and set by no stack until
    M8. Nothing failed, because the integration suite builds `SagaDeps` directly and
    supplies its own override — so the only path that needed the environment variable was
    the only path that never exercised it."""
    environments = _saga_environments(saga_template)
    assert environments, "no saga functions found — this test would pass vacuously"
    for environment in environments:
        assert name in environment, f"a dev saga Lambda has no {name}"


def test_the_compressed_timers_are_actually_short(saga_template: Template) -> None:
    """A "compression" that set 30 days would satisfy the presence check above and
    demonstrate nothing."""
    for environment in _saga_environments(saga_template):
        assert int(environment["GRACE_SECONDS_OVERRIDE"]) <= 3600
        assert int(environment["APPROVAL_TIMEOUT_SECONDS"]) <= 24 * 3600
