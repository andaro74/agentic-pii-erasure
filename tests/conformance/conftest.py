"""Preflight: refuse to grade a stack that is not running the code under test.

This exists because of V7-2. A participant defect (V7-1) was fixed, committed, and the
deployed gate re-run — against Lambdas still carrying the previous build, because nothing
between "edit source" and "assert behaviour" ever compared the two. The suite reported 16
failures identical to the ones the fix removed, which reads as *the fix did not work*
rather than *the fix was never deployed*. That is the costliest kind of wrong answer: it
sends you back to correct code.

CDK already computes exactly the fingerprint needed. The asset directory is hashed and the
hash becomes the Lambda's `Code.S3Key`, so comparing the locally synthesised template with
the deployed one answers "is the running code built from this working tree?" precisely,
with no fingerprinting scheme of our own and one read-only API call.

**The comparison is only as fresh as its inputs.** Synthesising from a stale staging
directory reproduces the deployed hash and reports agreement while testing old bytes —
that false pass is V7-2's actual mechanism, not a hypothetical. `make conformance`
therefore runs `package` and `synth` first; running pytest by hand without them is what
the explicit staleness message below is for.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[2]
STAGE = os.environ.get("PII_ERASURE_STAGE", "dev")

#: Stacks whose Lambda code must match the working tree. Grows with the milestones —
#: M5's saga executor belongs here the moment it exists.
CODE_BEARING_STACKS = ("participants",)

_REBUILD = "run `make deploy-dev` (it re-stages the asset and redeploys)"


def _synthesised(stack: str) -> dict[str, Any]:
    template = REPO / "infra" / "cdk.out" / f"asdp-{STAGE}-{stack}.template.json"
    if not template.is_file():
        pytest.fail(
            f"{template.relative_to(REPO)} is missing, so there is nothing to compare the "
            f"deployed stack against. Run `make package && make synth` first — or just use "
            f"`make conformance`, which does both."
        )
    return dict(json.loads(template.read_text(encoding="utf-8")))


def _deployed(stack: str) -> dict[str, Any] | None:
    try:
        body = boto3.client("cloudformation").get_template(StackName=f"asdp-{STAGE}-{stack}")
    except ClientError as error:
        if "does not exist" in str(error):
            return None
        raise
    return dict(body["TemplateBody"])


def _lambda_asset_keys(template: dict[str, Any]) -> dict[str, str]:
    """Logical id -> asset key, for functions this repo names.

    Restricted to resources carrying an explicit `FunctionName`, which excludes CDK's own
    auto-delete custom-resource handlers — those are aws-cdk-lib's code, not ours, and
    they legitimately differ when the CDK version moves.
    """
    return {
        logical: resource["Properties"]["Code"]["S3Key"]
        for logical, resource in template.get("Resources", {}).items()
        if resource.get("Type") == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName")
        and isinstance(resource["Properties"].get("Code", {}).get("S3Key"), str)
    }


@pytest.fixture(scope="session", autouse=True)
def deployed_code_matches_the_working_tree() -> None:
    """Fail the whole session before a single verb is graded, if the bytes disagree."""
    for stack in CODE_BEARING_STACKS:
        deployed = _deployed(stack)
        if deployed is None:
            # Not deployed at all is not this fixture's error to report: the per-participant
            # fixture skips with a milestone-aware reason, which stays the friendlier signal
            # for a stack that was never created.
            continue

        local_keys = _lambda_asset_keys(_synthesised(stack))
        deployed_keys = _lambda_asset_keys(deployed)
        stale = {
            logical: (deployed_keys.get(logical), key)
            for logical, key in local_keys.items()
            if deployed_keys.get(logical) != key
        }
        if stale:
            detail = "\n".join(
                f"  {logical}\n    deployed: {was}\n    working tree: {now}"
                for logical, (was, now) in sorted(stale.items())
            )
            pytest.fail(
                f"asdp-{STAGE}-{stack} is running code built from a different working "
                f"tree, so every result below would grade the wrong bytes (V7-2). "
                f"{_REBUILD}.\n{detail}",
                pytrace=False,
            )
