"""Structured allow/deny records. Feeds the adversarial eval (M7) and the ledger.

A decision is a fact about a *request*, so it carries `subject_ref` (pseudonymous) and
never the request body: tool inputs include artifact locators, and a denial log that
echoed them would put in CloudWatch exactly what the denial prevented from moving
(invariant 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyDecision:
    """One authorization outcome, from either backend."""

    decision: Decision
    action: str
    principal: str
    #: Cedar policy ids that determined the outcome. Empty on a default-deny, which is
    #: itself the signal: nothing matched, so nothing permitted.
    reasons: tuple[str, ...] = ()
    #: Where this verdict came from — "engine" (in-process pre-check) or "gateway".
    source: str = "engine"
    errors: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def default_deny(self) -> bool:
        """True when the denial came from nothing matching rather than a forbid.

        Worth distinguishing in the adversarial eval: a `forbid` firing means a rule
        caught something it was written for; a default-deny means the request was
        outside every permit, which is the injection case.
        """
        return self.decision is Decision.DENY and not self.reasons

    def log_fields(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "action": self.action,
            "principal": self.principal,
            "reasons": list(self.reasons),
            "source": self.source,
            "default_deny": self.default_deny,
            **self.context,
        }
