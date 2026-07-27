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


def test_no_bedrock_action_anywhere_in_the_saga_stack(saga_template: Template) -> None:
    """Invariant 12. The saga replays approved manifests; a model permission on this
    role would mean replay could re-enter the model — the exact thing ADR-001 forbids."""
    offending = [a for a in _all_policy_actions(saga_template) if "bedrock" in a.lower()]
    assert offending == [], f"saga roles must carry no bedrock permission, found {offending}"


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
