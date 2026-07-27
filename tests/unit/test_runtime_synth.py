"""M7's hermetic gate, second half: the Runtime role has no participant IAM.

The critical inversion this asserts: **the only compute allowed to call a model is the
only compute with no permission to touch subject data.** The discovery Runtime holds
Bedrock invocation, its own Memory namespace, and `InvokeGateway` — and nothing else.
Its single route to a participant is a Cedar-evaluated call through the Gateway.

Written as an assertion about **what the role has**, enumerated, rather than a list of
things it must not have. A denylist of forbidden actions passes for the wrong reason the
day someone grants `dynamodb:*` under a name the list never anticipated; an allowlist
fails on anything new, which for a role whose whole property is minimality is the
correct direction to fail in.

The `entryPoint` assertion is ADR-025 cost 3 made falsifiable: `entryPoint` is a filename
the control plane does not validate, so a rename deploys clean and fails at the first
invocation. Three things must agree — the Makefile's `RUNTIME_ENTRYPOINT`, the stack's
`ENTRYPOINT_FILE`, and a file that actually exists — and this pins all three.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App
from aws_cdk.assertions import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.foundation import FoundationStack
from stacks.gateway import GatewayStack
from stacks.participants import ParticipantsStack
from stacks.runtime import ENTRYPOINT_FILE, RUNTIME_VERSION, RuntimeStack

REPO = Path(__file__).resolve().parents[2]

#: Every service prefix a participant is reached through. If the Runtime role ever
#: gains an action in one of these, its "no participant IAM" property is gone — and
#: with it the claim that the reasoning plane cannot touch subject data directly.
PARTICIPANT_SERVICES = (
    "dynamodb",
    "s3",
    "cognito-idp",
    "rds-data",
    "ses",
    "sesv2",
    "glue",
    "athena",
    "s3vectors",
    "kms",
)

#: What the role legitimately holds. Anything outside this fails the test.
ALLOWED_PREFIXES = (
    "bedrock:InvokeModel",
    "bedrock-agentcore:InvokeGateway",
    "bedrock-agentcore:BatchCreateMemoryRecords",
    "bedrock-agentcore:RetrieveMemoryRecords",
    "bedrock-agentcore:ListMemoryRecords",
    "logs:",
    "cloudwatch:PutMetricData",
    "xray:",
    # The asset grant: it reads its own deployment package out of the CDK bucket.
    "s3:GetObject",
    "s3:GetBucket",
    "s3:List",
)


@pytest.fixture(scope="module")
def runtime_template() -> Template:
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
    gateway = GatewayStack(app, "asdp-t-gateway", stage="t", participants=participants.functions)
    runtime = RuntimeStack(
        app,
        "asdp-t-runtime",
        stage="t",
        gateway_arn=gateway.gateway.attr_gateway_arn,
        gateway_url=gateway.gateway.attr_gateway_url,
    )
    return Template.from_stack(runtime)


def _actions(template: Template) -> list[str]:
    found: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action", [])
            found.extend([action] if isinstance(action, str) else action)
    return found


# ─── the gate: no participant IAM ─────────────────────────────────────────────────────


def test_the_runtime_role_holds_no_participant_service_action(
    runtime_template: Template,
) -> None:
    """§9.3's claim, made structural. The reasoning plane reads broadly and writes
    nothing — and it cannot reach a participant except through the Gateway."""
    offending = [
        action
        for action in _actions(runtime_template)
        # The asset read is an s3: action on the CDK bucket, not on a participant's.
        # Excluded by name rather than by prefix so a real s3 grant still fails.
        if action.split(":")[0] in PARTICIPANT_SERVICES
        and not action.startswith(("s3:GetObject", "s3:GetBucket", "s3:List"))
    ]
    assert not offending, f"the discovery Runtime gained participant IAM: {sorted(set(offending))}"


def test_every_action_on_the_runtime_role_is_on_the_allowlist(
    runtime_template: Template,
) -> None:
    """An allowlist, not a denylist: for a role whose entire property is minimality,
    the safe failure direction is 'anything new fails'."""
    unexpected = [
        action for action in _actions(runtime_template) if not action.startswith(ALLOWED_PREFIXES)
    ]
    assert not unexpected, f"unexpected actions on the Runtime role: {sorted(set(unexpected))}"


def test_the_runtime_is_the_only_bedrock_caller(runtime_template: Template) -> None:
    """The mirror of invariant 12. The saga has no `bedrock:*` at all (asserted in
    `test_saga_synth.py`); this asserts the permission exists here, so the pair of
    tests says 'exactly one plane may reason' rather than each saying half of it."""
    bedrock = [a for a in _actions(runtime_template) if a.startswith("bedrock:")]
    assert bedrock, "the Runtime cannot invoke a model — it is the only plane that may"
    assert all(a.startswith("bedrock:InvokeModel") for a in bedrock)


def test_the_runtime_may_reach_the_gateway_and_only_the_gateway(
    runtime_template: Template,
) -> None:
    for policy in runtime_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") == "InvokeTheGatewayAndNothingElse":
                assert statement["Action"] == "bedrock-agentcore:InvokeGateway"
                return
    pytest.fail("no Gateway grant on the discovery Runtime role")


# ─── ADR-025: the zip, and the filename nobody validates ─────────────────────────────


def test_the_runtime_ships_code_not_a_container(runtime_template: Template) -> None:
    """ADR-025. A container would put a Docker daemon inside `cdk synth`, and
    `cdk synth` runs inside `make check`."""
    runtimes = runtime_template.find_resources("AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 1
    artifact = next(iter(runtimes.values()))["Properties"]["AgentRuntimeArtifact"]
    assert "CodeConfiguration" in artifact
    assert "ContainerConfiguration" not in artifact
    code = artifact["CodeConfiguration"]
    assert code["Runtime"] == RUNTIME_VERSION
    assert code["EntryPoint"] == [ENTRYPOINT_FILE]


def test_the_entrypoint_filename_matches_the_makefile_and_a_real_file() -> None:
    """ADR-025 cost 3: `entryPoint` is a filename the control plane does not check,
    so a rename deploys clean and fails at the first invocation. Three sources must
    agree, and this is the only place that can notice they stopped."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^RUNTIME_ENTRYPOINT := (\S+)$", makefile, re.MULTILINE)
    assert match, "RUNTIME_ENTRYPOINT is not declared in the Makefile"
    assert match.group(1) == ENTRYPOINT_FILE
    assert (REPO / "src" / "pii_erasure" / "runtime" / ENTRYPOINT_FILE).is_file()


def test_the_packaging_recipe_targets_arm64_with_binary_wheels_only() -> None:
    """ADR-025 cost 2, made visible. AgentCore Runtime is arm64-only, and numpy 2.3+
    publishes no `manylinux2014_aarch64` wheel — proven with `pip download` before a
    deploy rather than discovered during one. A recipe that lost `--only-binary` would
    silently build x86 or source-dist wheels that import fine here and fail there."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    platforms = re.search(r"^RUNTIME_PLATFORMS := (.+)$", makefile, re.MULTILINE)
    assert platforms, "RUNTIME_PLATFORMS is not declared"
    tags = platforms.group(1).split()
    assert "manylinux_2_28_aarch64" in tags, "numpy needs glibc 2.28+; the 2014 tag alone fails"
    assert all("aarch64" in tag for tag in tags), f"a non-arm64 tag would deploy broken: {tags}"
    runtime_block = makefile[makefile.index("$(RUNTIME_ASSET)") :]
    assert "--only-binary=:all:" in runtime_block


# ─── Memory ──────────────────────────────────────────────────────────────────────────


def test_memory_is_created_without_an_extraction_strategy(runtime_template: Template) -> None:
    """Invariant 13, expressed as an absence in the template.

    A memory strategy runs an extraction model over conversation events to decide what
    is worth remembering — from a transcript full of artifact locators and subject
    handles. Records are written explicitly by `discovery/memory.py` through a scrubber
    that rejects, so nothing reaches Memory that code a reviewer can read did not name.
    """
    memories = runtime_template.find_resources("AWS::BedrockAgentCore::Memory")
    assert len(memories) == 1
    properties = next(iter(memories.values()))["Properties"]
    assert not properties.get("MemoryStrategies"), (
        "an extraction strategy would let a model decide what lands in a cross-subject store"
    )


def test_the_runtime_memory_grant_is_scoped_to_its_own_store(
    runtime_template: Template,
) -> None:
    for policy in runtime_template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") == "TopologyPriors":
                assert statement["Resource"] != "*"
                return
    pytest.fail("no Memory grant found")


def test_nothing_in_the_runtime_stack_attaches_to_a_vpc(runtime_template: Template) -> None:
    """ADR-023's rule reaches the reasoning plane too. `PUBLIC` network mode also
    avoids NAT and interface endpoints, both of which bill continuously for existing."""
    runtimes = runtime_template.find_resources("AWS::BedrockAgentCore::Runtime")
    for resource in runtimes.values():
        network: dict[str, Any] = resource["Properties"]["NetworkConfiguration"]
        assert network["NetworkMode"] == "PUBLIC"
        assert "NetworkModeConfig" not in network
