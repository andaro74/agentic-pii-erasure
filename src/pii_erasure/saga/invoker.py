"""Direct Lambda invocation of participants — the M5 execution path.

Calls a participant exactly the way AgentCore Gateway does: the request body is the
event, and the verb travels in `ClientContext` as `bedrockAgentCoreToolName` — the same
path the conformance suite drives, so the saga exercises the entry point production
uses rather than a test-only one.

**This is the M5 → M6 seam.** At M6 the executor's mutating calls route through the
Gateway so AgentCore Policy evaluates Cedar on every call (§8.2); this invoker then
remains only as the shape both share. Until then the participant-side prechecks
(digest shape, token presence) are the in-band control, and the IAM boundary — the
executor may invoke exactly the eight participant functions and nothing else — is the
out-of-band one.

Error messages carry `system_id`, the tool, and the Lambda error *type* — never the
error payload, which can echo request fields (invariant 5).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import boto3


class ParticipantCallError(RuntimeError):
    """The participant invocation failed. Retryable by the caller's policy."""


class LambdaParticipantInvoker:
    """Invoke `asdp-<stage>-<systemId>` with the Gateway's calling convention."""

    def __init__(self, *, function_prefix: str, client: Any | None = None) -> None:
        self._prefix = function_prefix
        self._lambda = client or boto3.client("lambda")

    def call(self, system_id: str, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        context = base64.b64encode(
            json.dumps({"custom": {"bedrockAgentCoreToolName": f"{system_id}___{tool}"}}).encode()
        ).decode()
        try:
            response = self._lambda.invoke(
                FunctionName=f"{self._prefix}{system_id}",
                Payload=json.dumps(payload).encode(),
                ClientContext=context,
            )
        except Exception as error:  # botocore errors carry no request PII
            raise ParticipantCallError(
                f"{system_id}.{tool}: invocation failed ({type(error).__name__})"
            ) from error

        if "FunctionError" in response:
            body = json.loads(response["Payload"].read() or b"{}")
            raise ParticipantCallError(
                f"{system_id}.{tool}: participant raised {body.get('errorType', 'UnknownError')}"
            )
        result: dict[str, Any] = json.loads(response["Payload"].read())
        return result
