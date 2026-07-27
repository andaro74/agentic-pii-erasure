"""The in-process Cedar pre-check — a fast path and a test surface, NOT the control.

It evaluates **the same `.cedar` files** the Gateway deploys, through the same Cedar
engine (`cedarpy` wraps the Rust implementation Cedar is specified by), against a
schema reconstructed from the same tool manifest. So "the engine and the policies
agree" is not a claim maintained by hand — the divergence test in
`tests/unit/test_policies.py` drives both from one artifact and one corpus.

**Why it is not enforcement** (ADR-005, restated by ADR-018): in-process checks are
bypassable by any caller that forgets them, and they live inside the process an
injection is trying to influence. AgentCore Policy at the Gateway is authoritative.
This exists so a violation is caught before a network round-trip, and so the deny
corpus can be exercised in `make check` rather than only against a deployed stack.

No boto3, no framework: `policy/engine.py` is not on the invariant-0 allowlist and
must not become the reason it widens.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cedarpy import Decision as CedarDecision
from cedarpy import is_authorized, validate_policies

from pii_erasure.policy.decisions import Decision, PolicyDecision
from pii_erasure.policy.schema import (
    NAMESPACE,
    PRINCIPAL_TYPE,
    RESOURCE_TYPE,
    cedar_schema_json,
)

#: Where the deployed artifact lives. The stack reads these same files.
POLICY_DIR = Path(__file__).resolve().parents[3] / "policies" / "cedar"

#: Substituted into the policy text. Role names are stage-scoped, so a dev policy
#: cannot authorise a prod role that happens to share an account.
_STAGE_PLACEHOLDER = "{stage}"


class PolicyLoadError(RuntimeError):
    """The policy set is missing, empty, or does not validate against the schema."""


def policy_files() -> tuple[Path, ...]:
    """Every `.cedar` file, in filename order — the numeric prefixes are the order."""
    return tuple(sorted(POLICY_DIR.glob("*.cedar")))


def load_policy_text(stage: str, *, directory: Path | None = None) -> str:
    """Concatenate the policy set with `{stage}` resolved.

    Fails loudly on an empty set. A policy engine deployed with no policies denies
    everything by default, which looks like a working control right up until someone
    "fixes" the outage by switching enforcement off.
    """
    files = tuple(sorted((directory or POLICY_DIR).glob("*.cedar")))
    if not files:
        raise PolicyLoadError(f"no .cedar policies found in {directory or POLICY_DIR}")
    rendered = [
        path.read_text(encoding="utf-8").replace(_STAGE_PLACEHOLDER, stage) for path in files
    ]
    text = "\n".join(rendered)
    if _STAGE_PLACEHOLDER in text:
        raise PolicyLoadError("a placeholder survived rendering")
    return text


def validate(policy_text: str) -> list[str]:
    """Validate against the reconstructed schema. Returns the errors, empty if clean."""
    result = validate_policies(policy_text, cedar_schema_json())
    if result.validation_passed:
        return []
    return [error.error for error in result.errors]


def principal_uid(role_arn_or_name: str) -> str:
    """The Cedar principal for an IAM caller.

    AgentCore uses the *assumed-role* form — `arn:aws:sts::<account>:assumed-role/
    <role-name>` — which is stable across sessions, unlike the session-qualified ARN
    STS actually returns. Callers may pass either a full ARN or a bare role name.
    """
    return f'{NAMESPACE}::{PRINCIPAL_TYPE}::"{role_arn_or_name}"'


class PolicyEngine:
    """Evaluates the deployed policy set in-process."""

    def __init__(self, *, stage: str, gateway_arn: str, directory: Path | None = None) -> None:
        self._policies = load_policy_text(stage, directory=directory)
        self._gateway = gateway_arn
        self._schema = cedar_schema_json()
        errors = validate(self._policies)
        if errors:
            raise PolicyLoadError(
                f"the policy set does not validate against the tool manifest: {errors[0]}"
            )

    @property
    def policy_text(self) -> str:
        return self._policies

    def authorize(
        self,
        *,
        principal_arn: str,
        action: str,
        tool_input: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Evaluate one tool invocation. `action` is the wire name, e.g.
        ``profile-store___hard_delete``."""
        request = {
            "principal": principal_uid(principal_arn),
            "action": f'{NAMESPACE}::Action::"{action}"',
            "resource": f'{NAMESPACE}::{RESOURCE_TYPE}::"{self._gateway}"',
            "context": {"input": dict(tool_input or {})},
        }
        # The entities carry the attributes the policies read. `principal.id` is not
        # implied by the entity's UID: an entity supplied without attrs evaluates
        # `principal.id like …` to an error, every permit silently fails to match, and
        # the whole set default-denies — which looks exactly like a working deny-all.
        # AgentCore populates `id` with the caller's ARN; this mirrors that.
        entities = [
            {
                "uid": {"type": f"{NAMESPACE}::{PRINCIPAL_TYPE}", "id": principal_arn},
                "attrs": {"id": principal_arn},
                "parents": [],
            },
            {
                "uid": {"type": f"{NAMESPACE}::{RESOURCE_TYPE}", "id": self._gateway},
                "attrs": {},
                "parents": [],
            },
        ]
        result = is_authorized(request, self._policies, entities, self._schema)
        allowed = result.decision == CedarDecision.Allow
        diagnostics = result.diagnostics
        return PolicyDecision(
            decision=Decision.ALLOW if allowed else Decision.DENY,
            action=action,
            principal=principal_arn,
            reasons=tuple(diagnostics.reasons or ()),
            errors=tuple(str(e) for e in (diagnostics.errors or ())),
            source="engine",
        )

    def permitted_tools(self, *, principal_arn: str, tool_input: dict[str, Any]) -> tuple[str, ...]:
        """The actions this principal may invoke — the local mirror of what the
        Gateway's tool-list filtering returns. Used by the tool-surface assertions."""
        from pii_erasure.contract.registry import system_ids
        from pii_erasure.contract.tools import TOOL_NAMES, action_name

        return tuple(
            action
            for system_id in system_ids()
            for tool in TOOL_NAMES
            if (action := action_name(system_id, tool))
            and self.authorize(
                principal_arn=principal_arn, action=action, tool_input=tool_input
            ).allowed
        )


def schema_json() -> str:
    """Exposed so the stack can write the schema next to the policies for review."""
    return json.dumps(json.loads(cedar_schema_json()), indent=2, sort_keys=True)
