"""Interrupt payload construction and resume validation for the approval gate.

The payload is what the approver's tooling renders (§8.4): residual risk stated first,
blast radius as counts, the digest they are approving, and what becomes irreversible.
The resume is what comes back — and it must carry the digest, because approval binds
to the plan, not the subject (invariant 3).

This module is on the framework allowlist but deliberately imports no framework: the
`interrupt()` call itself lives in the saga node. Keeping payload and validation logic
framework-free means the approval *semantics* survive a framework migration even if
the pause mechanism changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pii_erasure.manifest import Manifest

#: Decisions a resume may carry. "timeout" arrives from the scheduler, not a human —
#: silence implies DENY (§8.2).
APPROVAL_DECISIONS = frozenset({"approve", "deny", "timeout"})

#: Gate identifiers, carried in every interrupt payload so the resume handler can tell
#: a stale wake from a live one (invariant 11's subtle half: an approval-timeout that
#: fires *after* approval must not resume the grace-window interrupt).
GATE_APPROVAL = "approval"
GATE_GRACE = "grace_window"
GATE_SWEEP = "sweep"
#: Phase 3 ran out of forward road; a human remediates and resumes (§5, "Stuck").
GATE_STUCK = "stuck"


class ResumeValidationError(ValueError):
    """The resume value is not a valid answer to the question the gate asked."""


@dataclass(frozen=True)
class ApprovalResume:
    decision: str
    digest: str | None
    approver: str


def interrupt_payload(manifest: Manifest) -> dict[str, Any]:
    """What the approver sees. Pseudonymous throughout — invariant 5 applies here too."""
    residuals = [
        {"kind": r.kind, "locator": r.locator, "reason": r.reason} for r in manifest.residual_risk
    ]
    blast_radius = [
        {
            "systemId": p.system_id,
            "archetype": p.archetype.value,
            "artifactCount": sum(a.count for a in p.artifacts),
            "plannedOps": list(p.planned_ops),
            "deleteMethod": p.delete_method,
        }
        for p in manifest.participants
    ]
    return {
        "gate": GATE_APPROVAL,
        "expectedWakes": ["approval_timeout"],
        "manifestDigest": manifest.digest,
        "sagaId": manifest.saga_id,
        "subjectRef": manifest.subject_ref,
        # Residual risk first (§8.4): what will NOT be deleted leads, because that is
        # the part of the plan a rubber stamp misses.
        "residualRisk": residuals,
        "blastRadius": blast_radius,
        "legalHolds": [h.digested_body() for h in manifest.legal_holds],
        "graceWindowDays": manifest.grace_window_days,
        "irreversible": [
            p.system_id for p in manifest.participants if p.delete_method == "CRYPTO_SHRED"
        ],
    }


def parse_resume(value: Any, *, expected_digest: str) -> ApprovalResume:
    """Validate a resume value against the gate's question.

    A mismatched digest is not an error in the caller's *format* — it is the TOCTOU
    signal (§8.3), so it gets its own message and the saga aborts to re-approval rather
    than proceeding.
    """
    if not isinstance(value, dict):
        raise ResumeValidationError("approval resume must be an object")
    decision = value.get("decision")
    if decision not in APPROVAL_DECISIONS:
        raise ResumeValidationError(
            f"decision must be one of {sorted(APPROVAL_DECISIONS)}, got {decision!r}"
        )
    digest = value.get("digest")
    approver = str(value.get("approver", "")) or "unknown"
    if decision == "approve":
        if not digest:
            raise ResumeValidationError(
                "an approval must carry the digest the approver reviewed (invariant 3)"
            )
        if digest != expected_digest:
            raise ResumeValidationError(
                "approval digest does not match the signed manifest — the plan changed "
                "after review; a new manifest requires a new approval"
            )
    return ApprovalResume(decision=str(decision), digest=digest, approver=approver)
