"""Invariant 5's mechanism, tested from M0 (ROADMAP M0 trap).

The load-bearing assertions: an email NEVER survives the scrubber — not in a
string, not nested in a mapping, not smuggled through a log processor — and the
rejecting form fails loudly without echoing the value it rejected.
"""

from __future__ import annotations

import pytest
import structlog

from pii_erasure.observability.logging import configure_logging
from pii_erasure.observability.redact import (
    REDACTED,
    PIIRejectedError,
    reject_if_pii,
    scrub,
    scrub_mapping,
)

EMAILS = [
    "marisol.okonkwo@example.com",
    "d.vasquez-lund+dsr@sub.example.co.uk",
    "YUKI_ABRAMSON@EXAMPLE.ORG",
]


@pytest.mark.parametrize("email", EMAILS)
def test_email_never_survives_scrub(email: str) -> None:
    scrubbed = scrub(f"subject wrote {email} into the bio field")
    assert email not in scrubbed
    assert email.lower() not in scrubbed.lower()
    assert REDACTED in scrubbed


def test_phone_is_scrubbed_but_small_integers_survive() -> None:
    assert "+44 20 7946 0958" not in scrub("call +44 20 7946 0958 now")
    # counts and ports must survive — a scrubber that eats them is unusable
    assert scrub("deleted 412 rows on port 5432") == "deleted 412 rows on port 5432"


def test_pseudonymous_handles_survive_unchanged() -> None:
    line = "saga saga_01JQ8 processed sub_a3f9 with digest sha256:ab12"
    assert scrub(line) == line


def test_mapping_denylisted_keys_redacted_and_nested_values_scrubbed() -> None:
    payload = {
        "subject_ref": "sub_a3f9",
        "name": "Marisol Okonkwo",  # denylisted key: redacted wholesale
        "detail": {"note": "reached at marisol.okonkwo@example.com", "count": 412},
        "tags": ["ok", "mail: d.vasquez-lund+dsr@sub.example.co.uk"],
    }
    out = scrub_mapping(payload)
    assert out["subject_ref"] == "sub_a3f9"
    assert out["name"] == REDACTED
    assert "example.com" not in out["detail"]["note"]
    assert out["detail"]["count"] == 412
    assert "example.co.uk" not in out["tags"][1]


def test_email_never_survives_the_log_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    """The processor chain is the belt: even a caller who forgot the scrubber
    exists cannot emit an email through structlog."""
    configure_logging("INFO")
    structlog.get_logger("test").info(
        "discovery note", bio="contact marisol.okonkwo@example.com", subject_ref="sub_a3f9"
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "marisol.okonkwo@example.com" not in combined
    assert "sub_a3f9" in combined


def test_reject_if_pii_fails_loudly_without_echoing_the_value() -> None:
    with pytest.raises(PIIRejectedError) as excinfo:
        reject_if_pii("tenant meridian admin is marisol.okonkwo@example.com")
    assert "example.com" not in str(excinfo.value)  # the message must not leak it
    assert excinfo.value.rule == "email"


def test_reject_if_pii_passes_topology_shaped_text_through() -> None:
    text = "vector-index mirrors profile-store in tenant meridian"
    assert reject_if_pii(text) == text


# ─── structural identifiers survive scrubbing (V11-6) ─────────────────────────────────


def test_a_digest_survives_scrubbing() -> None:
    """Invariant 3 binds approval to exactly these bytes.

    The phone rule matches any run of 7+ digits, and a hex digest contains one about 64%
    of the time — so `sha256:4e074085…` became `sha256:4e[REDACTED]bedb…`. The approval
    API then compared what the operator echoed against the real digest and refused a
    legitimate approval as a changed plan. Intermittently, which is worse than always.
    """
    import hashlib

    mangled = [
        digest
        for digest in (f"sha256:{hashlib.sha256(str(i).encode()).hexdigest()}" for i in range(300))
        if scrub(digest) != digest
    ]
    assert not mangled, f"{len(mangled)}/300 digests corrupted, e.g. {mangled[:1]}"


def test_a_bare_hex_digest_survives() -> None:
    """Idempotency keys are raw sha256 hex with no prefix, and they are compared for
    equality on the replay path — a redacted one is a double-apply."""
    import hashlib

    key = hashlib.sha256(b"saga|system|op").hexdigest()
    assert scrub(key) == key


def test_an_arn_survives_scrubbing() -> None:
    """A 12-digit AWS account id is phone-shaped. Redacting it breaks the signing-key
    correlation an auditor follows from a manifest to CloudTrail."""
    arn = "arn:aws:kms:us-west-2:581208540944:key/9c1e-abc"
    assert scrub(arn) == arn


def test_preservation_does_not_shelter_adjacent_pii() -> None:
    """The risk of an exemption is that it becomes a hiding place. Only the identifier
    itself is preserved; the text around it is scrubbed normally."""
    text = "subject ada@example.invalid at sha256:" + "ab12" * 16 + " called +1 415 555 0123"
    scrubbed = scrub(text)
    assert "ada@example.invalid" not in scrubbed
    assert "+1 415 555 0123" not in scrubbed
    assert "sha256:" + "ab12" * 16 in scrubbed


def test_pii_inside_a_mapping_beside_a_digest_is_still_redacted() -> None:
    payload = {
        "manifestDigest": "sha256:" + "0123456789abcdef" * 4,
        "email": "grace@example.invalid",
        "note": "reach me on +1 415 555 0123",
    }
    out = scrub_mapping(payload)
    assert out["manifestDigest"] == payload["manifestDigest"]
    assert out["email"] == REDACTED
    assert "555" not in out["note"]


def test_a_long_digit_run_that_is_not_an_identifier_is_still_scrubbed() -> None:
    """The exemption is shape-specific, not a general amnesty for digits."""
    assert scrub("account 4111111111111111 please") != "account 4111111111111111 please"
