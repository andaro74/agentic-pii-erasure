"""The walkthrough's own assertions must be able to pass, and to fail.

M8's deployed gate is a script, and a script that grades a deployment is a gate — so it
inherits the rule the rest of this repo lives by: a check that cannot go red is
decoration, and a check that cannot go green is worse, because it fails a system that
worked and sends someone hunting.

Both defects were live here at once (V11-4):

* `_certificate` required lowercase `"approval_granted"` while the nodes emit
  `APPROVAL_GRANTED`. It could never have matched. A flawless run would have failed at
  step 8 with "no ledger entry evidencing the deletion", which is the most alarming
  possible way to be wrong.
* `seeded_subject` did not exist: the walkthrough invented `sub_<random>`, discovery
  correctly found nothing, and the saga died building a manifest with zero participants.

So this file pins the walkthrough's expectations against the code that produces them —
the ledger event names are read out of `saga/nodes/`, not restated — and asserts the
subject comes from the generated map rather than from a UUID.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from pii_erasure.cli import walkthrough
from pii_erasure.cli.operations import OperationError

REPO = Path(__file__).resolve().parents[2]
NODES = REPO / "src" / "pii_erasure" / "saga" / "nodes"


def _emitted_event_types() -> set[str]:
    """Every `event_type="..."` the saga nodes actually append."""
    found: set[str] = set()
    for path in NODES.glob("*.py"):
        found.update(re.findall(r'event_type="([A-Z_]+)"', path.read_text(encoding="utf-8")))
    assert len(found) > 10, "the event-type scan found almost nothing — wrong directory?"
    return found


@pytest.mark.parametrize("event", sorted(walkthrough.REQUIRED_LEDGER_EVENTS))
def test_the_certificate_requires_events_the_saga_can_emit(event: str) -> None:
    """The bug, in one assertion. The check compared lowercase against UPPERCASE, so
    every required event was unsatisfiable and a perfect run would have been failed."""
    assert event in _emitted_event_types(), (
        f"the walkthrough demands a ledger entry {event!r} that no node emits — "
        f"a gate that cannot pass"
    )


def test_the_certificate_demands_evidence_of_the_irreversible_step() -> None:
    """A certificate that does not evidence the hard delete is a receipt for nothing."""
    assert "HARD_DELETE_APPLIED" in walkthrough.REQUIRED_LEDGER_EVENTS
    assert "APPROVAL_GRANTED" in walkthrough.REQUIRED_LEDGER_EVENTS


class _Entry:
    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        self.saga_id = "saga_1"
        self.seq = 0


def test_a_missing_ledger_entry_fails_the_walkthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: the check must still be able to go red."""
    entries = [_Entry("APPROVAL_GRANTED"), _Entry("SAGA_COMPLETED")]
    monkeypatch.setattr(walkthrough.operations, "verify_ledger", lambda saga: (2, entries))
    with pytest.raises(OperationError, match="HARD_DELETE_APPLIED"):
        walkthrough._certificate("saga_1", {"residual_count": 0})


def test_a_complete_ledger_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_Entry(name) for name in walkthrough.REQUIRED_LEDGER_EVENTS]
    monkeypatch.setattr(walkthrough.operations, "verify_ledger", lambda saga: (3, entries))
    walkthrough._certificate("saga_1", {"residual_count": 1})


def test_an_empty_ledger_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(walkthrough.operations, "verify_ledger", lambda saga: (0, []))
    with pytest.raises(OperationError, match="no ledger entries"):
        walkthrough._certificate("saga_1", {})


# ─── the subject must have data behind it ─────────────────────────────────────────────


def test_the_subject_comes_from_the_generated_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inventing `sub_<uuid>` and expecting data behind it was the original defect: the
    plan came back empty and the saga died 37 seconds in."""
    truth = tmp_path / "ground-truth.json"
    truth.write_text(
        json.dumps(
            {
                "subjects": {
                    "sub_thin": {"profile-store": {}},
                    "sub_rich": {"profile-store": {}, "upload-bucket": {}, "vector-index": {}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(walkthrough, "GROUND_TRUTH", truth)
    monkeypatch.setattr(walkthrough, "_tombstoned", lambda refs: set())
    assert walkthrough.seeded_subject() == "sub_rich"


def test_a_missing_map_says_to_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(walkthrough, "GROUND_TRUTH", tmp_path / "absent.json")
    with pytest.raises(OperationError, match="make seed"):
        walkthrough.seeded_subject()


def test_a_map_with_no_placements_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty map is the shape a failed seed leaves behind, and picking from it would
    reproduce exactly the empty-plan crash this function exists to prevent."""
    truth = tmp_path / "ground-truth.json"
    truth.write_text(json.dumps({"subjects": {"sub_a": {}}}), encoding="utf-8")
    monkeypatch.setattr(walkthrough, "GROUND_TRUTH", truth)
    monkeypatch.setattr(walkthrough, "_tombstoned", lambda refs: set())
    with pytest.raises(OperationError, match="no placed artifacts"):
        walkthrough.seeded_subject()


def test_an_explicit_subject_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--subject` must still win: an operator debugging one subject should not be
    silently redirected to whichever one the fixtures liked best."""
    captured: dict[str, Any] = {}

    def fake_arc(*, saga_id: str, subject_ref: str, tenant: str) -> None:
        captured["subject"] = subject_ref

    monkeypatch.setattr(walkthrough, "_arc", fake_arc)
    assert walkthrough.run(subject="sub_chosen") == 0
    assert captured["subject"] == "sub_chosen"


# ─── the second run must not target the first run's subject ───────────────────────────


def _map(tmp_path: Path, subjects: dict[str, dict[str, Any]]) -> Path:
    truth = tmp_path / "ground-truth.json"
    truth.write_text(json.dumps({"subjects": subjects}), encoding="utf-8")
    return truth


def test_an_already_erased_subject_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """M8's gate is "twice, identically", and this function is deterministic — `max()`
    over the same map returns the same subject. So the second run would have targeted
    the subject the first run had just erased: discovery finds nothing, and `intake`
    refuses on the tombstone before that. Neither is a platform bug; both look like one.
    """
    monkeypatch.setattr(
        walkthrough,
        "GROUND_TRUTH",
        _map(
            tmp_path,
            {
                "sub_erased": {"a": {}, "b": {}, "c": {}},
                "sub_fresh": {"a": {}, "b": {}},
            },
        ),
    )
    monkeypatch.setattr(walkthrough, "_tombstoned", lambda refs: {"sub_erased"})
    assert walkthrough.seeded_subject() == "sub_fresh"


def test_exhausted_fixtures_say_to_reseed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Re-running against a tombstoned subject tests the resurrection guard, not the
    walkthrough — so the message must send the operator to `make seed`, not leave them
    reading a phase-2 failure."""
    monkeypatch.setattr(walkthrough, "GROUND_TRUTH", _map(tmp_path, {"sub_a": {"a": {}}}))
    monkeypatch.setattr(walkthrough, "_tombstoned", lambda refs: set(refs))
    with pytest.raises(OperationError, match="make seed"):
        walkthrough.seeded_subject()


def test_the_richest_remaining_subject_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Breadth still matters among the survivors: phase 3's per-participant loop is where
    ordering and residual honesty actually show up."""
    monkeypatch.setattr(
        walkthrough,
        "GROUND_TRUTH",
        _map(tmp_path, {"sub_one": {"a": {}}, "sub_many": {"a": {}, "b": {}, "c": {}}}),
    )
    monkeypatch.setattr(walkthrough, "_tombstoned", lambda refs: set())
    assert walkthrough.seeded_subject() == "sub_many"
