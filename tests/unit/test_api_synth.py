"""The operator API's IAM and auth shape, asserted against the synthesised template.

One unauthenticated route on this API is the human-in-the-loop control gone: an approval
nobody made, recorded in the ledger as one that somebody did. That is not a property to
review by eye on the day the route is added — the route that breaks it will be added
months later, by someone adding a feature, and it will look completely reasonable.

So the checks read the template rather than the source:

| Claim | Read from |
|---|---|
| Every route carries an authorizer | `AWS::ApiGatewayV2::Route.AuthorizerId` |
| The route set is exactly what we intended | Route keys vs a verbatim list |
| Operators are not data subjects | The authorizer's pool is in this stack, not participants' |
| The API can reach the saga and nothing else | The role's inline policy actions |
| No participant data plane | Absence, listed rather than assumed |

The last two follow `test_runtime_synth.py`'s pattern: a "no X" claim is checked by
enumerating what the role *has*, because a test asserting the absence of something it
never looks for passes trivially.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "infra"))


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    from aws_cdk import App, assertions
    from stacks.api import ApiStack
    from stacks.foundation import FoundationStack
    from stacks.participants import ParticipantsStack
    from stacks.saga import SagaStack

    app = App(context={"stage": "test"})
    foundation = FoundationStack(app, "asdp-test-foundation", stage="test", object_lock_days=1)
    participants = ParticipantsStack(
        app,
        "asdp-test-participants",
        stage="test",
        object_lock_days=1,
        dek_registry=foundation.dek_registry,
        idempotency=foundation.idempotency,
    )
    saga = SagaStack(
        app,
        "asdp-test-saga",
        stage="test",
        checkpoints=foundation.checkpoints,
        checkpoint_offload=foundation.checkpoint_offload,
        ledger=foundation.ledger,
        tombstones=foundation.tombstones,
        idempotency=foundation.idempotency,
        signing_key=foundation.signing_key,
        participants=participants.functions,
    )
    api = ApiStack(app, "asdp-test-api", stage="test", saga_executor=saga.executor_fn)
    return assertions.Template.from_stack(api).to_json()  # type: ignore[no-any-return]


def _resources(template: dict[str, Any], type_name: str) -> dict[str, Any]:
    return {
        logical_id: body
        for logical_id, body in template["Resources"].items()
        if body["Type"] == type_name
    }


# ─── 1. every route is authenticated ──────────────────────────────────────────────────


def test_every_route_carries_an_authorizer(template: dict[str, Any]) -> None:
    """The claim the docstring makes, checked against the template that deploys."""
    routes = _resources(template, "AWS::ApiGatewayV2::Route")
    assert routes, "no routes synthesised — this test would pass vacuously"
    unauthenticated = [
        body["Properties"].get("RouteKey")
        for body in routes.values()
        if not body["Properties"].get("AuthorizerId")
    ]
    assert not unauthenticated, f"routes with no authorizer: {unauthenticated}"


def test_every_route_uses_the_jwt_authorization_type(template: dict[str, Any]) -> None:
    """An `AuthorizerId` with `AuthorizationType: NONE` would satisfy the test above and
    authorise nothing — the pairing is what enforces, not either field alone."""
    for body in _resources(template, "AWS::ApiGatewayV2::Route").values():
        assert body["Properties"].get("AuthorizationType") == "JWT", body["Properties"]


#: Written out verbatim, NOT derived from `stacks.api.ROUTES`. The first version of this
#: test compared the template against that constant and a mutation adding `GET /health` to
#: it passed — both sides moved together, so the test graded nothing. The point of an
#: expected-surface test is that widening the surface requires touching a second file, on
#: purpose; the same reasoning `test_import_boundary.py` gives for naming the framework
#: allowlist word for word.
EXPECTED_ROUTES = frozenset(
    {
        "POST /requests",
        "GET /threads",
        "GET /threads/{sagaId}",
        "POST /threads/{sagaId}/approve",
    }
)


def test_the_route_set_is_exactly_what_was_intended(template: dict[str, Any]) -> None:
    """A route added without a matching entry here is a surface nobody reviewed.

    The authorizer test above still covers a new route's *authentication* — this one
    covers its existence, which is the part a default cannot make safe.
    """
    synthesised = {
        body["Properties"]["RouteKey"]
        for body in _resources(template, "AWS::ApiGatewayV2::Route").values()
    }
    assert synthesised == EXPECTED_ROUTES


def test_the_authorizer_is_a_jwt_authorizer(template: dict[str, Any]) -> None:
    authorizers = _resources(template, "AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    body = next(iter(authorizers.values()))["Properties"]
    assert body["AuthorizerType"] == "JWT"
    assert body["IdentitySource"] == ["$request.header.Authorization"]


# ─── 2. operators are not data subjects ───────────────────────────────────────────────


def test_the_operator_pool_is_owned_by_this_stack(template: dict[str, Any]) -> None:
    """`cognito-identity` owns a pool of *subjects* — the people being erased. If the
    approval API trusted that pool, a data subject could obtain a token for it, and
    approving one's own erasure is the mildest thing that would then be possible."""
    pools = _resources(template, "AWS::Cognito::UserPool")
    assert len(pools) == 1, "the API stack must own exactly its own operator pool"
    assert next(iter(pools.values()))["Properties"]["UserPoolName"] == "asdp-test-operators"


def test_the_pool_forbids_self_signup(template: dict[str, Any]) -> None:
    """Open registration on the pool that authorises deletion is self-service approval."""
    pool = next(iter(_resources(template, "AWS::Cognito::UserPool").values()))["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


def test_both_approver_groups_exist(template: dict[str, Any]) -> None:
    """T3's two-person rule needs two groups to distinguish; one group cannot express
    "a second, different human"."""
    from stacks.api import APPROVER_GROUP, LEGAL_GROUP

    groups = {
        body["Properties"]["GroupName"]
        for body in _resources(template, "AWS::Cognito::UserPoolGroup").values()
    }
    assert groups == {APPROVER_GROUP, LEGAL_GROUP}


def test_the_group_names_match_the_handler(template: dict[str, Any]) -> None:
    """Two constants, two files. They must agree or nobody can approve anything."""
    from stacks.api import APPROVER_GROUP, LEGAL_GROUP

    from pii_erasure.approval import api as handler

    assert handler.APPROVER_GROUP == APPROVER_GROUP
    assert handler.LEGAL_GROUP == LEGAL_GROUP


# ─── 3. the front door reaches the saga and nothing else ──────────────────────────────


def _api_role_actions(template: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    for body in _resources(template, "AWS::IAM::Policy").values():
        for statement in body["Properties"]["PolicyDocument"]["Statement"]:
            raw = statement.get("Action", [])
            actions.update([raw] if isinstance(raw, str) else raw)
    return actions


def test_the_api_can_invoke_the_saga(template: dict[str, Any]) -> None:
    assert "lambda:InvokeFunction" in _api_role_actions(template)


@pytest.mark.parametrize(
    "forbidden",
    ["dynamodb:", "kms:", "s3:", "bedrock:", "bedrock-agentcore:", "cognito-idp:AdminDelete"],
)
def test_the_api_holds_no_data_plane_permission(template: dict[str, Any], forbidden: str) -> None:
    """Enumerated rather than assumed. The blast radius of a compromised front door is
    "can ask the saga for things the saga already validates" — it is not a second path to
    the participants."""
    granted = sorted(a for a in _api_role_actions(template) if a.startswith(forbidden))
    assert not granted, f"the approval API was granted {granted}"


def test_the_api_lambda_has_no_vpc(template: dict[str, Any]) -> None:
    """Nothing in this platform attaches to a VPC (ADR-023)."""
    for body in _resources(template, "AWS::Lambda::Function").values():
        assert not body["Properties"].get("VpcConfig"), body["Properties"].get("FunctionName")


def test_the_handler_points_at_the_module_that_exists(template: dict[str, Any]) -> None:
    """A handler string is a filename the deploy does not validate — it fails at the
    first request instead, which is the same class as ADR-025's entrypoint trap."""
    import importlib

    functions = _resources(template, "AWS::Lambda::Function")
    handler = next(iter(functions.values()))["Properties"]["Handler"]
    module_path, _, attribute = handler.rpartition(".")
    assert hasattr(importlib.import_module(module_path), attribute)
