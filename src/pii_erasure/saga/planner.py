"""The saga's window onto the reasoning plane — invariant 2's boundary, made concrete.

`nodes/plan.py` is the single node permitted to talk to the reasoning plane, and this is
*how* it talks: it invokes the AgentCore Runtime over HTTP and **receives a manifest
body**. It never holds a model client, never sees a prompt, never branches on model
output. The saga's whole relationship with the model is "ask for a plan, get JSON back".

That is not a stylistic preference. It is what makes invariant 12 enforceable in IAM:
the `saga-executor` role carries no `bedrock:*` at all, only
`bedrock-agentcore:InvokeAgentRuntime` on one ARN — asserted in `cdk synth`. If the saga
held a model client, no IAM policy could express the difference between "reasoning about
a plan" and "reasoning about whether to delete", and the boundary would be a code-review
rule again.

**Replay never re-enters the model.** This is called once, by `plan`, on the first pass.
A resumed saga replays the checkpointed manifest; there is no code path from a resume to
a fresh Runtime invocation, because a re-plan under a prior approval would execute a plan
nobody approved (invariant 3).
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

#: The service rejects a `runtimeSessionId` shorter than 33 characters. Padding here
#: rather than at the call site because the failure is a 400 from a service that is
#: otherwise working, which reads like a bug in the payload.
_MIN_SESSION_ID = 33


class DiscoveryPlanner(Protocol):
    """What `plan` needs from the reasoning plane, and the whole of it."""

    def plan(self, *, subject_ref: str, saga_id: str, tenant: str) -> dict[str, Any]: ...


class RuntimePlanner:
    """Invokes the deployed discovery Runtime and returns a candidate manifest body."""

    def __init__(
        self,
        runtime_arn: str,
        *,
        client: Any | None = None,
        qualifier: str = "DEFAULT",
    ) -> None:
        self._runtime_arn = runtime_arn
        self._qualifier = qualifier
        if client is None:
            import boto3

            client = boto3.client("bedrock-agentcore")
        self._client = client

    def plan(self, *, subject_ref: str, saga_id: str, tenant: str = "default") -> dict[str, Any]:
        response = self._client.invoke_agent_runtime(
            agentRuntimeArn=self._runtime_arn,
            qualifier=self._qualifier,
            runtimeSessionId=session_id(saga_id),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(
                {"subjectRef": subject_ref, "sagaId": saga_id, "tenant": tenant}
            ).encode(),
        )
        body = response["response"].read()
        parsed: dict[str, Any] = json.loads(body)
        return parsed


def session_id(saga_id: str) -> str:
    """A deterministic, contract-legal `runtimeSessionId` for one saga.

    Deterministic so a retried invocation lands on the same session rather than
    provisioning a second microVM — and derived from `sagaId`, which is already the
    trace-correlation key (`thread_id` == `sagaId` == trace id).

    Never enters a digested body: `provenance.runtimeSessionId` is excluded from
    canonicalisation precisely because it is volatile, and a session id inside the
    digest would make identical plans produce different digests (invariant 4).
    """
    padded = f"asdp-{saga_id}".ljust(_MIN_SESSION_ID, "0")
    return padded[:64]


def planner_from_environment() -> RuntimePlanner | None:
    """Build a planner if the Runtime ARN is configured; `None` otherwise.

    `None` is a legitimate configuration and not a degraded one: an M5-shaped saga
    replays a manifest supplied in its start input, which is how the execution plane
    stayed fully testable before discovery existed (ADR-001). `plan` fails loudly when
    *neither* a manifest nor a planner is available — it never invents one.
    """
    arn = os.environ.get("DISCOVERY_RUNTIME_ARN", "")
    return RuntimePlanner(arn) if arn else None
