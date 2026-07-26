"""PII scrubber — invariant 5's mechanism.

Every log line, trace attribute, exception message, ledger entry, and AgentCore
Memory write passes through :func:`scrub` (or :func:`scrub_mapping`). The seeded
fake PII is treated exactly as if it were real; that discipline is part of what
the repo demonstrates.

M0 skeleton scope: emails, E.164-ish phone numbers, and a denylist of mapping
keys whose *values* are PII by construction. Later milestones extend the pattern
set (M4 seeds, M7 memory pre-write) — they extend it here, in one place, so
there is never a second scrubber to drift from this one.

Design choice, deliberate: :func:`scrub_mapping` REDACTS suspect values rather
than dropping keys, and :func:`reject_if_pii` FAILS LOUDLY rather than
sanitising. The Memory pre-write path (ADR-019) uses the rejecting form —
silently storing a near-miss is the failure mode, not the save.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Ordered: applied top to bottom. Email first so a phone-shaped substring inside
# an address is handled by the email rule, not half-mangled by the phone rule.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # E.164 and common separator forms, 7+ digits total. Deliberately does not
    # match bare short integers (counts, ports) — precision matters in logs.
    ("phone", re.compile(r"\+?\d[\d\s().-]{5,}\d(?=\D|$)")),
)

# Mapping keys whose values are PII by construction, regardless of shape.
# Compared case-insensitively after stripping [-_ ].
_DENY_KEYS = frozenset(
    {"name", "fullname", "givenname", "familyname", "email", "phone", "address", "dob", "birthdate"}
)

# subject_ref / sagaId style handles are pseudonymous and MUST survive scrubbing
# unchanged — a scrubber that eats the correlation key is as useless as one that
# leaks PII. Enforced by test, not just intent.


class PIIRejectedError(ValueError):
    """Raised by :func:`reject_if_pii` — the write is refused, not sanitised."""

    def __init__(self, rule: str) -> None:
        # Deliberately does NOT echo the offending value: this exception's
        # message ends up in logs, which is exactly where the value must not go.
        super().__init__(f"payload rejected by PII rule {rule!r}")
        self.rule = rule


def scrub(text: str) -> str:
    """Return *text* with every recognised PII form replaced by ``[REDACTED]``."""
    for _rule, pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _key_is_denied(key: str) -> bool:
    return key.lower().replace("-", "").replace("_", "").replace(" ", "") in _DENY_KEYS


def scrub_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub a mapping recursively: denylisted keys are redacted wholesale,
    string values are pattern-scrubbed, nested mappings/lists recurse."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if _key_is_denied(key):
            out[key] = REDACTED
        else:
            out[key] = _scrub_value(value)
    return out


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        return scrub_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    return value


def reject_if_pii(text: str) -> str:
    """Return *text* unchanged iff no PII rule fires; raise otherwise.

    The AgentCore Memory pre-write path (ADR-019, invariant 13) uses this form:
    a topology prior that trips a rule is a bug to surface, never data to store.
    """
    for rule, pattern in _PATTERNS:
        if pattern.search(text):
            raise PIIRejectedError(rule)
    return text
