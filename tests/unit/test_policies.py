"""`make policy-test` — the Cedar policy set parses, validates, and decides correctly.

Three layers, and the middle one is the point:

1. **Parse + schema-validate.** Every `.cedar` file validates against the Cedar schema
   reconstructed from the tool manifest. This is ADR-018's requirement run in
   milliseconds. The authoritative version is AWS's own `FAIL_ON_ANY_FINDINGS` at
   deploy; this one exists so the feedback loop is not measured in dollars.
2. **Decide.** A corpus of requests with expected verdicts, driven through the real
   Cedar engine over the real deployed artifact. A policy set that parses but permits
   the wrong thing is worse than one that fails to parse.
3. **Drift.** The action enumerations are compared against the registry, so
   participant #9 cannot be silently unprotected — the same shape as
   `test_conformance_coverage.py`.

There is deliberately **no divergence test between an in-process rule engine and the
`.cedar` files**, because there is no second rule set to diverge from: `PolicyEngine`
evaluates *these files*, through Cedar, rather than reimplementing them in Python.
ADR-005 and ADR-018 anticipated a hand-written subset and a test to keep the two
honest; removing the second implementation removes the drift instead of policing it.
ADR-024 records that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pii_erasure.contract.registry import system_ids
from pii_erasure.contract.tools import (
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    TOOL_NAMES,
    action_name,
    action_names,
)
from pii_erasure.policy.engine import (
    POLICY_DIR,
    PolicyEngine,
    PolicyLoadError,
    load_policy_text,
    policy_files,
    validate,
)
from pii_erasure.policy.schema import cedar_schema

STAGE = "dev"
ACCOUNT = "000000000000"
GATEWAY = f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:gateway/asdp-{STAGE}-gateway"

DISCOVERY = f"arn:aws:sts::{ACCOUNT}:assumed-role/asdp-{STAGE}-discovery"
EXECUTOR = f"arn:aws:sts::{ACCOUNT}:assumed-role/asdp-{STAGE}-saga-executor"
STRANGER = f"arn:aws:sts::{ACCOUNT}:assumed-role/some-other-role"

DIGEST = "sha256:" + "a" * 64
READ_INPUT: dict[str, Any] = {"subjectRef": "sub_x", "sagaId": "saga_x"}
MUTATE_INPUT: dict[str, Any] = {
    "subjectRef": "sub_x",
    "sagaId": "saga_x",
    "manifestDigest": DIGEST,
    "idempotencyKey": "k",
}
HARD_INPUT: dict[str, Any] = {**MUTATE_INPUT, "approvalToken": "tok"}


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine(stage=STAGE, gateway_arn=GATEWAY)


# ─── 1. parse and validate ────────────────────────────────────────────────────────────


def test_the_policy_set_exists_and_is_not_empty() -> None:
    """An engine deployed with no policies denies everything, which looks exactly like
    a working control until someone restores service by switching enforcement off."""
    assert len(policy_files()) >= 4, "the policy set is missing files"


def test_every_policy_validates_against_the_generated_schema() -> None:
    errors = validate(load_policy_text(STAGE, gateway_arn=GATEWAY))
    assert errors == [], f"policy set does not validate: {errors[0] if errors else ''}"


@pytest.mark.parametrize("path", policy_files(), ids=lambda p: p.stem)
def test_each_file_holds_exactly_one_statement(path: Path) -> None:
    """AgentCore's CreatePolicy takes ONE Cedar statement, so the file boundary is the
    policy boundary. Two statements in a file would deploy as one truncated policy."""
    body = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    )
    assert body.count(";") == 1, f"{path.name} must contain exactly one Cedar statement"


@pytest.mark.parametrize("path", policy_files(), ids=lambda p: p.stem)
def test_every_policy_names_the_specific_gateway(path: Path) -> None:
    """V10-3's mechanism guard. CreatePolicy rejects a tool-specific action list scoped
    to `resource is AgentCore::Gateway` — "a constrained action scope was encountered,
    please constrain the resource to a specific AgentCore::Gateway resource". Verified
    the expensive way; kept red-able the cheap way."""
    body = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
    )
    assert 'resource == AgentCore::Gateway::"{gateway_arn}"' in body, (
        f"{path.name} does not pin the gateway"
    )
    assert "resource is AgentCore::Gateway" not in body, (
        f"{path.name} uses the type-only resource form the control plane rejects"
    )


def test_a_permit_for_one_gateway_does_not_authorise_another() -> None:
    """What the pinning buys: the policy set is an artifact of ONE gateway. A second
    gateway in the same account — another stage's, another team's — inherits nothing."""
    from cedarpy import Decision as CedarDecision
    from cedarpy import is_authorized

    from pii_erasure.policy.schema import cedar_schema_json

    other = GATEWAY.replace("asdp-dev-gateway", "another-teams-gateway")
    request = {
        "principal": f'AgentCore::IamEntity::"{DISCOVERY}"',
        "action": f'AgentCore::Action::"{action_name("profile-store", "discover")}"',
        "resource": f'AgentCore::Gateway::"{other}"',
        "context": {"input": dict(READ_INPUT)},
    }
    entities = [
        {
            "uid": {"type": "AgentCore::IamEntity", "id": DISCOVERY},
            "attrs": {"id": DISCOVERY},
            "parents": [],
        },
        {"uid": {"type": "AgentCore::Gateway", "id": other}, "attrs": {}, "parents": []},
    ]
    result = is_authorized(
        request, load_policy_text(STAGE, gateway_arn=GATEWAY), entities, cedar_schema_json()
    )
    assert result.decision != CedarDecision.Allow


def test_rendering_leaves_no_placeholder() -> None:
    text = load_policy_text("prod", gateway_arn=GATEWAY)
    assert "{stage}" not in text
    assert "{gateway_arn}" not in text
    assert "asdp-prod-discovery" in text
    assert f'AgentCore::Gateway::"{GATEWAY}"' in text


def test_an_empty_policy_directory_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(PolicyLoadError, match=r"no \.cedar"):
        load_policy_text(STAGE, gateway_arn=GATEWAY, directory=tmp_path)


def test_an_invented_context_key_would_fail_validation() -> None:
    """The guard that makes guard 2 meaningful: prove the validator can go red.

    ARCHITECTURE §9.2's illustrative set reads `context.legalHoldCount`. AgentCore
    injects only `context.input`, so that policy would deploy against nothing and
    never fire — the defect ADR-018 exists to prevent, reproduced here on purpose.
    """
    bogus = (
        "permit(principal is AgentCore::IamEntity, action == AgentCore::Action::"
        f'"{action_name(system_ids()[0], "hard_delete")}", '
        f'resource == AgentCore::Gateway::"{GATEWAY}")'
        " when { context.legalHoldCount == 0 };"
    )
    errors = validate(bogus)
    assert errors, "the validator accepted a context key AgentCore never injects"
    assert "legalHoldCount" in errors[0]


# ─── 2. decide ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("system_id", system_ids())
def test_discovery_may_read_every_participant(engine: PolicyEngine, system_id: str) -> None:
    for tool in sorted(READ_ONLY_TOOLS):
        decision = engine.authorize(
            principal_arn=DISCOVERY, action=action_name(system_id, tool), tool_input=READ_INPUT
        )
        assert decision.allowed, f"discovery denied {tool} on {system_id}"


@pytest.mark.parametrize("system_id", system_ids())
def test_discovery_may_never_mutate_anything(engine: PolicyEngine, system_id: str) -> None:
    """Invariant 1, as a policy property, for every participant."""
    for tool in sorted(MUTATING_TOOLS):
        decision = engine.authorize(
            principal_arn=DISCOVERY, action=action_name(system_id, tool), tool_input=HARD_INPUT
        )
        assert not decision.allowed, f"discovery was permitted {tool} on {system_id}"


def test_the_discovery_tool_surface_is_exactly_discover_and_verify(
    engine: PolicyEngine,
) -> None:
    """The local mirror of what `tools/list` returns for this identity — the
    `tool_surface_minimality` claim, checkable without a deployed Gateway."""
    permitted = engine.permitted_tools(principal_arn=DISCOVERY, tool_input=READ_INPUT)
    assert set(permitted) == set(action_names(READ_ONLY_TOOLS))
    assert len(permitted) == len(system_ids()) * len(READ_ONLY_TOOLS)


def test_the_executor_may_operate(engine: PolicyEngine) -> None:
    for system_id in system_ids():
        assert engine.authorize(
            principal_arn=EXECUTOR,
            action=action_name(system_id, "soft_delete"),
            tool_input=MUTATE_INPUT,
        ).allowed
        assert engine.authorize(
            principal_arn=EXECUTOR,
            action=action_name(system_id, "hard_delete"),
            tool_input=HARD_INPUT,
        ).allowed


def test_hard_delete_without_an_approval_token_is_denied(engine: PolicyEngine) -> None:
    """M6's deployed gate, hermetically. Forbid-wins: no principal is exempt."""
    for principal in (EXECUTOR, DISCOVERY, STRANGER):
        decision = engine.authorize(
            principal_arn=principal,
            action=action_name("profile-store", "hard_delete"),
            tool_input={**MUTATE_INPUT, "approvalToken": ""},
        )
        assert not decision.allowed, f"{principal} hard-deleted with an empty token"


def test_a_mutation_without_a_well_formed_digest_is_denied(engine: PolicyEngine) -> None:
    for bad in ("", "not-a-digest", "md5:abc"):
        decision = engine.authorize(
            principal_arn=EXECUTOR,
            action=action_name("profile-store", "soft_delete"),
            tool_input={**MUTATE_INPUT, "manifestDigest": bad},
        )
        assert not decision.allowed, f"a mutation was permitted with digest {bad!r}"


def test_an_unknown_principal_gets_nothing(engine: PolicyEngine) -> None:
    """Default-deny, and it is a *default* deny — nothing matched at all."""
    decision = engine.authorize(
        principal_arn=STRANGER,
        action=action_name("profile-store", "discover"),
        tool_input=READ_INPUT,
    )
    assert not decision.allowed
    assert decision.default_deny
    assert engine.permitted_tools(principal_arn=STRANGER, tool_input=READ_INPUT) == ()


def test_a_neighbouring_stage_cannot_borrow_this_stages_permits() -> None:
    """Role names are stage-scoped, so a dev policy must not authorise a prod role
    that happens to live in the same account."""
    prod_engine = PolicyEngine(stage="prod", gateway_arn=GATEWAY)
    decision = prod_engine.authorize(
        principal_arn=DISCOVERY,  # the *dev* discovery role
        action=action_name("profile-store", "discover"),
        tool_input=READ_INPUT,
    )
    assert not decision.allowed


# ─── 3. drift ─────────────────────────────────────────────────────────────────────────


def test_the_policy_actions_cover_the_registry_exactly() -> None:
    """Participant #9 must not arrive unprotected — nor must a removed one linger."""
    text = load_policy_text(STAGE, gateway_arn=GATEWAY)
    for system_id in system_ids():
        for tool in TOOL_NAMES:
            action = action_name(system_id, tool)
            assert f'"{action}"' in text, f"no policy mentions {action}"


def test_the_schema_covers_every_action_the_gateway_publishes() -> None:
    actions = cedar_schema()["AgentCore"]["actions"]
    assert set(actions) == set(action_names(TOOL_NAMES))
    for definition in actions.values():
        context = definition["appliesTo"]["context"]["attributes"]
        assert set(context) == {"input"}, "AgentCore injects only context.input"


def test_the_schema_declares_the_binding_fields_policies_rely_on() -> None:
    """A policy reading `context.input.manifestDigest` is only meaningful if the tool
    manifest declares it — otherwise the field is never populated and the check is
    vacuous rather than false."""
    actions = cedar_schema()["AgentCore"]["actions"]
    hard = actions[action_name("profile-store", "hard_delete")]
    attributes = hard["appliesTo"]["context"]["attributes"]["input"]["attributes"]
    assert "manifestDigest" in attributes
    assert "approvalToken" in attributes
    read = actions[action_name("profile-store", "discover")]
    read_attributes = read["appliesTo"]["context"]["attributes"]["input"]["attributes"]
    assert "approvalToken" not in read_attributes, "a read verb must not carry a token"


def test_the_policy_directory_is_where_the_stack_looks() -> None:
    assert POLICY_DIR.is_dir()
    assert {p.name for p in policy_files()} == {p.name for p in POLICY_DIR.glob("*.cedar")}
