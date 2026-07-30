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
#
# That principle was written here at M0 and tested for `subject_ref` alone. Two other
# structural identifiers turned out to need it just as much, and both were being
# corrupted (V11-6):
#
#   * **sha256 digests.** The phone rule matches any run of 7+ digits, and a hex digest
#     contains such runs about 64% of the time. `sha256:4e074085…` became
#     `sha256:4e[REDACTED]bedb…`. Invariant 3 binds approval to exactly these bytes, so a
#     mangled digest is not a cosmetic defect — the approval API compared what the
#     operator echoed against the real digest and refused a legitimate approval as a
#     changed plan. Intermittently, which is worse than always.
#   * **AWS ARNs.** A 12-digit account id is phone-shaped:
#     `arn:aws:kms:us-west-2:[REDACTED]:key/…`. That breaks the signing-key correlation
#     an auditor follows.
#
# Neither can carry PII: a 64-char hex string and an ARN have no room for a name or an
# address. So they are matched FIRST and copied through untouched, and only the text
# between them is scrubbed — an email beside a digest is still redacted.
_PRESERVED = re.compile(
    r"""(?:
        sha256:[0-9a-fA-F]{64}      # digests, prefixed
      | \b[0-9a-f]{64}\b            # digests and idempotency keys, bare
      | arn:aws[a-z0-9-]*:[^\s"',]+ # ARNs, including the 12-digit account id
    )""",
    re.VERBOSE,
)


class PIIRejectedError(ValueError):
    """Raised by :func:`reject_if_pii` — the write is refused, not sanitised."""

    def __init__(self, rule: str) -> None:
        # Deliberately does NOT echo the offending value: this exception's
        # message ends up in logs, which is exactly where the value must not go.
        super().__init__(f"payload rejected by PII rule {rule!r}")
        self.rule = rule


def scrub(text: str) -> str:
    """Return *text* with every recognised PII form replaced by ``[REDACTED]``.

    Structural identifiers — digests, ARNs — are carried through verbatim; everything
    around them is scrubbed normally. See `_PRESERVED` for why that is safe and why it
    is necessary.
    """
    out: list[str] = []
    cursor = 0
    for match in _PRESERVED.finditer(text):
        out.append(_apply_patterns(text[cursor : match.start()]))
        out.append(match.group(0))
        cursor = match.end()
    out.append(_apply_patterns(text[cursor:]))
    return "".join(out)


def _apply_patterns(text: str) -> str:
    for _rule, pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _key_is_denied(key: str) -> bool:
    return key.lower().replace("-", "").replace("_", "").replace(" ", "") in _DENY_KEYS


#: CloudWatch's Embedded Metric Format metadata node. Passed through untouched.
#:
#: EMF puts the metric *name* in a member called `Name`, and `name` is a denied key —
#: correctly, for a person's name. So `{"Name": "resurrection.detected"}` was redacted
#: wholesale, CloudWatch extracted a metric literally called `[REDACTED]`, and every alarm
#: on a real metric name would have sat in INSUFFICIENT_DATA forever: an alarm that cannot
#: fire, which is worse than no alarm because a dashboard shows it as healthy (V13-3).
#:
#: Exempting the node rather than the key is what makes this safe. `_aws` contains only
#: metadata and *references*: `Namespace`, `Timestamp`, dimension **key names**, metric
#: names and units. Every dimension and metric **value** lives on the root node, outside
#: this subtree, and is still scrubbed. There is nowhere in here for a name or an address
#: to hide.
#:
#: The same shape as V11-6, which is why the fix is a class and not a special case: a
#: structural value that must survive scrubbing intact, corrupted by a rule written for
#: free text. Dimension values additionally must stay low-cardinality — EMF bills a custom
#: metric per unique combination — so `subjectRef` is barred from them by cost and by
#: invariant 5 at once.
_EMF_METADATA_KEY = "_aws"


def scrub_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub a mapping recursively: denylisted keys are redacted wholesale,
    string values are pattern-scrubbed, nested mappings/lists recurse.

    The `_aws` EMF metadata node is copied through verbatim — see above.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key == _EMF_METADATA_KEY:
            out[key] = value
        elif _key_is_denied(key):
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
