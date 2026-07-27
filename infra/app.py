"""CDK app entrypoint.

Environment-agnostic on purpose: `make synth` runs in `make check` on every
commit with no AWS account, so nothing here may perform an account lookup.

`stage` comes from CDK context (`--context stage=…`), which the Makefile feeds
from $STAGE / $PII_ERASURE_STAGE — CI passes a per-run value so ephemeral eval
stacks are actually ephemeral (VALIDATION finding V3-1).
"""

from __future__ import annotations

import os

from aws_cdk import App
from stacks.foundation import FoundationStack
from stacks.gateway import GatewayStack
from stacks.participants import ParticipantsStack
from stacks.runtime import RuntimeStack
from stacks.saga import SagaStack

app = App()

stage: str = app.node.try_get_context("stage") or "dev"

# COMPLIANCE-mode Object Lock cannot be shortened or overridden after write —
# not by root, not by anyone. Prod pins the ledger's statutory 7 years; every
# other stage reads a short window from the environment so the bucket can
# actually be torn down (infra/README.md leads with this hazard).
_SEVEN_YEARS_DAYS = 2557
object_lock_days: int = (
    _SEVEN_YEARS_DAYS
    if stage == "prod"
    else int(os.environ.get("PII_ERASURE_DEV_OBJECT_LOCK_DAYS", "1"))
)

foundation = FoundationStack(
    app,
    f"asdp-{stage}-foundation",
    stage=stage,
    object_lock_days=object_lock_days,
)

participants = ParticipantsStack(
    app,
    f"asdp-{stage}-participants",
    stage=stage,
    object_lock_days=object_lock_days,
    dek_registry=foundation.dek_registry,
    idempotency=foundation.idempotency,
)


gateway = GatewayStack(
    app,
    f"asdp-{stage}-gateway",
    stage=stage,
    # The stack owns this mapping. Listing the participants again here would be a second
    # place to forget one, and a participant missing from the Gateway is a participant the
    # agent cannot reach — a recall failure with no error attached.
    participants=participants.functions,
)

# The reasoning plane (M7). Takes the Gateway's ARN and URL and nothing else: the
# Runtime's only route to subject data is through the Gateway, so there is nothing
# else for it to be handed.
runtime = RuntimeStack(
    app,
    f"asdp-{stage}-runtime",
    stage=stage,
    gateway_arn=gateway.gateway.attr_gateway_arn,
    gateway_url=gateway.gateway.attr_gateway_url,
)

SagaStack(
    app,
    f"asdp-{stage}-saga",
    stage=stage,
    checkpoints=foundation.checkpoints,
    checkpoint_offload=foundation.checkpoint_offload,
    ledger=foundation.ledger,
    tombstones=foundation.tombstones,
    idempotency=foundation.idempotency,
    signing_key=foundation.signing_key,
    participants=participants.functions,
    # The one AgentCore permission invariant 12 allows the saga: `plan` invokes
    # this Runtime and receives a manifest. Passed as an exact ARN rather than a
    # pattern, which is why the runtime stack is constructed first.
    discovery_runtime_arn=runtime.runtime.attr_agent_runtime_arn,
)

app.synth()
