"""M6's deployed-gate probe: speak MCP to the Gateway and show what Cedar does to you.

Nothing in this repo traverses the Gateway yet — the saga invokes participant Lambdas
directly (`saga/invoker.py`), and the discovery Runtime that will be the Gateway's first
real client lands at M7. So M6's deployed gate is exercised the only way it can be:
this script signs MCP requests with YOUR credentials and reports what the policy layer
does with them. Your identity appears in no Cedar permit, which makes you the perfect
probe — everything you can do is something a stranger can do.

Two probes, two claims:

1. ``tools/list`` — in ENFORCE, an unpermitted identity sees an EMPTY tool surface.
   Deny-by-default made visible: not "your call was rejected" but "there is nothing
   here for you". The discovery-identity half of the original gate (exactly the two
   read verbs per target) needs a caller that can BE `asdp-discovery`, and that role
   trusts only `bedrock-agentcore.amazonaws.com` — it is asserted hermetically today
   (`test_the_discovery_tool_surface_is_exactly_discover_and_verify`) and lands
   deployed with M7's `tool_surface_minimality` evaluator.

2. ``tools/call profile-store___hard_delete`` with an EMPTY approval token — in
   ENFORCE, denied at the Gateway; the participant Lambda is never invoked. In
   LOG_ONLY the Gateway forwards it and the PARTICIPANT's precheck refuses instead
   (the M4 conformance property) — same outcome, wrong layer, which is why LOG_ONLY
   cannot pass this gate and the script says so rather than pretending.

The probe subject does not exist and the token is empty, so nothing can mutate even if
every control were absent — but the point of running it is to watch the controls fire.

Read-only with respect to real data; costs nothing but the HTTPS calls. Usage:

    python scripts/verify_policy_gate.py [--stage dev]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

#: 5 verbs x 8 participants — what an unfiltered surface looks like in LOG_ONLY.
FULL_SURFACE = 40

_PROBE_HARD_DELETE = {
    "subjectRef": "sub_probe_never_existed",
    "sagaId": "saga_policy_probe",
    "manifestDigest": "sha256:" + "0" * 64,
    "idempotencyKey": "policy-gate-probe",
    "approvalToken": "",  # the point of the probe
}


def _outputs(stage: str) -> dict[str, str]:
    stacks = boto3.client("cloudformation").describe_stacks(StackName=f"asdp-{stage}-gateway")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks["Stacks"][0]["Outputs"]}


def _mcp(url: str, region: str, method: str, params: dict[str, Any], rid: int) -> dict[str, Any]:
    """One SigV4-signed MCP call. Returns the parsed JSON-RPC envelope; HTTP-level
    denials are folded into the same shape so the caller sees one kind of thing."""
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    request = AWSRequest(
        method="POST",
        url=url,
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    session = boto3.Session()
    credentials = session.get_credentials()
    assert credentials is not None, "no AWS credentials in the environment"
    SigV4Auth(credentials.get_frozen_credentials(), "bedrock-agentcore", region).add_auth(request)
    prepared = urllib.request.Request(  # noqa: S310 — the URL comes from stack outputs
        url, data=request.body, headers=dict(request.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(prepared, timeout=30) as response:  # noqa: S310
            return _parse(response.read().decode(), response.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as error:
        text = error.read().decode(errors="replace")
        return {"httpStatus": error.code, "error": {"message": text}}


def _parse(text: str, content_type: str) -> dict[str, Any]:
    """The streamable-HTTP transport may answer as SSE; the payload is the data line."""
    if "text/event-stream" in content_type:
        data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data_lines[-1] if data_lines else "{}"
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _list_tools(url: str, region: str) -> tuple[list[str], dict[str, Any]]:
    tools: list[str] = []
    cursor: str | None = None
    last: dict[str, Any] = {}
    for page in range(1, 20):
        params: dict[str, Any] = {"cursor": cursor} if cursor else {}
        last = _mcp(url, region, "tools/list", params, rid=page)
        result = last.get("result") or {}
        tools.extend(tool["name"] for tool in result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor or "error" in last:
            break
    return tools, last


def _looks_denied(envelope: dict[str, Any]) -> bool:
    text = json.dumps(envelope).lower()
    return any(marker in text for marker in ("denied", "unauthorized", "not authorized", "forbid"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    stage = parser.parse_args().stage

    region = boto3.Session().region_name
    if not region:
        print("FAIL  no AWS region configured (AWS_REGION / profile)")
        return 1
    outs = _outputs(stage)
    url, mode = outs["GatewayUrl"], outs["PolicyMode"]
    caller = boto3.client("sts").get_caller_identity()["Arn"]
    print(f"gateway : {url}")
    print(f"mode    : {mode}")
    print(f"caller  : {caller}  (appears in no Cedar permit — a stranger by design)")
    print()

    tools, raw_list = _list_tools(url, region)
    call = _mcp(
        url,
        region,
        "tools/call",
        {"name": "profile-store___hard_delete", "arguments": dict(_PROBE_HARD_DELETE)},
        rid=99,
    )

    if mode != "ENFORCE":
        # LOG_ONLY evaluates and records but blocks nothing, so the gate's two claims
        # are not assertable — say so instead of letting a green-looking run imply it.
        print(f"tools/list  : {len(tools)} tools visible (unfiltered; {FULL_SURFACE} expected)")
        print(f"hard_delete : {json.dumps(call)[:300]}")
        print()
        print("LOG_ONLY: the Gateway forwards everything; the refusal above (if any) came")
        print("from the PARTICIPANT's precheck — right outcome, wrong layer for this gate.")
        print("Flip and re-run:  POLICY_MODE=ENFORCE make deploy-dev  &&  re-run this probe")
        return 1

    surface_hidden = tools == []
    denied = _looks_denied(call) or _looks_denied(raw_list)
    print(f"tools/list  : {len(tools)} tools visible -> {'PASS' if surface_hidden else 'FAIL'}"
          f" (an unpermitted identity must see an empty surface)")
    if not surface_hidden:
        print(f"    visible: {sorted(tools)[:6]} ...")
    print(f"hard_delete : {'PASS — denied at the Gateway' if denied else 'FAIL'}")
    print(f"    response: {json.dumps(call)[:300]}")
    return 0 if (surface_hidden and denied) else 1


if __name__ == "__main__":
    sys.exit(main())
