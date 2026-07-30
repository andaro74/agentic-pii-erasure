"""ADR-021's rule, made mechanical: no resource may bill for *existing* without an ADR.

CLAUDE.md states it as a hard constraint — *"Nothing in the stack bills continuously for
existing rather than for working"* — and ADR-021 gives the procedure: *"Before adding any
AWS service, check whether it has a provisioned floor — if it does, it needs an ADR arguing
why the floor is worth it."* That was a rule for a human to remember at review time, which
is the weakest kind. The whole reason S3 Vectors replaced OpenSearch Serverless was an OCU
floor nobody noticed until the bill arrived.

So the check reads the synthesised templates and fails on any floor-bearing resource type
that is not in `ACCEPTED` with an ADR behind it. Two properties make it worth having rather
than reassuring:

* **The denylist names services this repo does not use.** A check that only listed what is
  already here would pass forever and catch nothing — the point is the *next* service
  somebody reaches for.
* **An acceptance can carry a condition.** Aurora is allowed only while its serverless
  scaling floor is genuinely zero. "We use Aurora Serverless v2" is not the property that
  makes it free; `MinCapacity: 0` is, and that is what gets asserted.

Writing this check surfaced one violation of the hard constraint that nothing had recorded
— see `ACCEPTED["AWS::SecretsManager::Secret"]` and VALIDATION.md V13-4.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SYNTH = REPO / "infra" / "cdk.out"
ADR_DIR = REPO / "docs" / "adr"

#: Resource types that cost money for existing, whether or not anything uses them. Most of
#: these the repo has never used — deliberately, because the value of this list is the
#: service somebody adds next year, not the six already here.
FLOOR_BEARING: dict[str, str] = {
    "AWS::OpenSearchServerless::Collection": "OCU floor — the reason ADR-021 exists",
    "AWS::OpenSearchService::Domain": "per-node hourly, whether queried or not",
    "AWS::EC2::NatGateway": "hourly per gateway, plus data processing",
    "AWS::EC2::Instance": "hourly while running",
    "AWS::ElastiCache::CacheCluster": "per-node hourly",
    "AWS::ElastiCache::ReplicationGroup": "per-node hourly",
    "AWS::ElastiCache::ServerlessCache": "minimum stored-data charge",
    "AWS::MSK::Cluster": "per-broker hourly",
    "AWS::MSK::ServerlessCluster": "per-partition hourly",
    "AWS::Redshift::Cluster": "per-node hourly",
    "AWS::RedshiftServerless::Namespace": "base RPU charge",
    "AWS::Neptune::DBCluster": "per-instance hourly",
    "AWS::DocDB::DBCluster": "per-instance hourly",
    "AWS::EKS::Cluster": "flat hourly control-plane charge",
    "AWS::ECS::Service": "task hours — Fargate is what ADR-015 removed",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "hourly plus LCU",
    "AWS::ElasticLoadBalancing::LoadBalancer": "hourly",
    "AWS::EC2::VPCEndpoint": "interface endpoints bill hourly per AZ",
    "AWS::EC2::TransitGateway": "hourly per attachment",
    "AWS::GlobalAccelerator::Accelerator": "fixed daily charge",
    "AWS::Kinesis::Stream": "shard hours unless ON_DEMAND",
    "AWS::FSx::FileSystem": "provisioned capacity",
    "AWS::DirectoryService::MicrosoftAD": "hourly per directory",
    "AWS::RDS::DBCluster": "hourly per instance unless serverless with a zero floor",
    "AWS::RDS::DBInstance": "hourly unless db.serverless with a zero-floor cluster",
    "AWS::SecretsManager::Secret": "$0.40 per secret per month, used or not",
    "AWS::WAFv2::WebACL": "monthly per ACL plus per rule",
    "AWS::CloudTrail::Trail": "the first trail is free; data events are not",
}

#: Floors this platform accepts, each with the ADR that argued for it. A type here without
#: a live ADR file fails the test — the citation has to resolve, or it is decoration.
ACCEPTED: dict[str, tuple[str, str]] = {
    "AWS::RDS::DBCluster": (
        "ADR-023-aurora-needs-a-vpc.md",
        "Aurora Serverless v2 at min_capacity = 0 auto-pauses to no compute charge. The "
        "acceptance is CONDITIONAL and the condition is asserted below: a cluster with a "
        "non-zero floor is a different decision than the one ADR-023 recorded.",
    ),
    "AWS::RDS::DBInstance": (
        "ADR-023-aurora-needs-a-vpc.md",
        "The writer of that cluster — db.serverless, so it scales with the cluster and has "
        "no fixed instance class to bill for.",
    ),
    "AWS::SecretsManager::Secret": (
        "ADR-023-aurora-needs-a-vpc.md",
        "$0.40 per month, and the only thing in the stack that bills purely for existing "
        "(V13-4). Forced rather than chosen: the RDS Data API authenticates with a Secrets "
        "Manager ARN, and the Data API is precisely what keeps every Lambda out of a VPC. "
        "The alternative — credentials in an environment variable — trades $0.40 for a "
        "secret in plaintext in a template, which is not a trade worth making.",
    ),
}


def _templates() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SYNTH.glob("asdp-*.template.json"))
    }


def _resources(kind: str) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for name, template in _templates().items():
        for logical, body in template.get("Resources", {}).items():
            if body.get("Type") == kind:
                found.append((f"{name}:{logical}", body.get("Properties", {})))
    return found


@pytest.fixture(scope="module", autouse=True)
def synthesised() -> None:
    """`make check` runs `cdk synth` before the tests, so the templates are present. If
    they are not, say so rather than passing over an empty directory."""
    if not _templates():
        pytest.fail(
            f"no synthesised templates in {SYNTH}. Run `make synth` — this check reads the "
            f"template, because that is what CloudFormation acts on."
        )


def test_the_denylist_covers_services_this_repo_does_not_use() -> None:
    """A list of only what is already here would pass forever. The value is the service
    added next, by someone who has not read ADR-021."""
    present = {
        body["Type"]
        for template in _templates().values()
        for body in template["Resources"].values()
    }
    unused = set(FLOOR_BEARING) - present
    assert len(unused) >= 15, (
        f"only {len(unused)} floor-bearing types are unused, so this check is mostly "
        f"describing the status quo rather than guarding against additions"
    )


def test_no_resource_bills_a_floor_without_an_adr() -> None:
    """The rule ADR-021 stated for humans, applied by the build."""
    offenders: list[str] = []
    for kind, why in FLOOR_BEARING.items():
        if kind in ACCEPTED:
            continue
        for where, _properties in _resources(kind):
            offenders.append(f"{where} is {kind} ({why})")
    assert not offenders, (
        "these resources bill for existing and no ADR argues why that is worth it:\n  "
        + "\n  ".join(offenders)
        + "\n\nADR-021's rule: check for a provisioned floor BEFORE adding a service, and "
        "if it has one, write the ADR. Adding it to ACCEPTED without one is the same "
        "defect with extra steps."
    )


@pytest.mark.parametrize("kind", sorted(ACCEPTED))
def test_every_accepted_floor_cites_an_adr_that_exists(kind: str) -> None:
    """A citation that does not resolve is worse than none: it looks like a decision."""
    adr, reason = ACCEPTED[kind]
    assert (ADR_DIR / adr).is_file(), f"{kind} cites {adr}, which is not in docs/adr/"
    assert len(reason) > 80, f"{kind}'s acceptance does not argue anything"


def test_aurora_really_has_a_zero_floor() -> None:
    """The condition the RDS acceptance rests on.

    ADR-021's whole argument is that an idle stack costs cents. `min_capacity = 0` is what
    makes that true of Aurora; "we chose Serverless v2" does not. A floor of 0.5 ACU would
    be about $43/month per cluster and would pass any check that only looked at the engine.
    """
    clusters = _resources("AWS::RDS::DBCluster")
    assert clusters, "no Aurora cluster synthesised — has the participant moved?"
    for where, properties in clusters:
        scaling = properties.get("ServerlessV2ScalingConfiguration")
        assert scaling, f"{where} is not a Serverless v2 cluster"
        assert scaling["MinCapacity"] == 0, (
            f"{where} has a floor of {scaling['MinCapacity']} ACU. That is a continuous "
            f"charge for existing, and it needs a superseding ADR rather than a test edit."
        )

    for where, properties in _resources("AWS::RDS::DBInstance"):
        assert properties.get("DBInstanceClass") == "db.serverless", (
            f"{where} has a fixed instance class, which bills hourly regardless of the "
            f"cluster's scaling configuration"
        )


def test_nothing_we_run_attaches_to_a_vpc_via_a_nat_gateway() -> None:
    """ADR-023 states it plainly: a VPC exists solely to hold the cluster — isolated
    subnets, no NAT, no endpoints, no continuous cost. The NAT gateway is the part that
    would bill hourly, so its absence is the claim worth asserting."""
    assert not _resources("AWS::EC2::NatGateway")
    assert not _resources("AWS::EC2::VPCEndpoint")
