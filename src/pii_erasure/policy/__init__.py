"""Authorization at the tool boundary — Cedar, evaluated outside the model.

AgentCore Policy inside the Gateway is the authoritative control (ADR-018); the
in-process engine here is a fast pre-check and a test surface, never enforcement.
What Cedar can and cannot express against the real generated schema is recorded in
[ADR-024](../../../docs/adr/ADR-024-cedar-expresses-identity-and-shape.md).

Framework-free except `middleware.py` (invariant 0).
"""

from pii_erasure.policy.decisions import Decision, PolicyDecision
from pii_erasure.policy.engine import PolicyEngine, load_policy_text
from pii_erasure.policy.schema import cedar_schema, cedar_schema_json

__all__ = [
    "Decision",
    "PolicyDecision",
    "PolicyEngine",
    "cedar_schema",
    "cedar_schema_json",
    "load_policy_text",
]
