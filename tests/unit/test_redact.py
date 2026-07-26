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
