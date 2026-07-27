"""The conformance suite must be able to seed — and tear down — every participant.

Until M4's second half, `tests/conformance` could seed only two of the eight registered
participants; the other six skipped with "no conformance seeder yet". Structurally that
was V8-3's shape: the deployed gate would have reported 16 passed / 48 skipped and read
as green while proving nothing about six participants. A skip that looks like coverage
is the failure mode, and it was invisible to `make check` because nothing hermetic
compared the seeding table against the registry.

These tests close that seam. They import the conformance module (import-safe: nothing
in it touches AWS at module level) and assert its tables cover the registry exactly —
so registering participant #9 without a placement and a cleanup fails the build, here,
rather than skipping quietly in the one suite that costs money to run.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from pii_erasure.contract.registry import system_ids

REPO = Path(__file__).resolve().parents[2]
CONFORMANCE = REPO / "tests" / "conformance" / "test_contract.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("conformance_under_test", CONFORMANCE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_registered_participant_has_a_placement() -> None:
    module = _load()
    placements = module.PLACEMENTS  # type: ignore[attr-defined]

    assert set(placements) == set(system_ids()), (
        "PLACEMENTS and the registry disagree — a participant without a placement "
        "skips the deployed gate while appearing covered (V8-3's shape)"
    )
    for system_id, artifacts in placements.items():
        assert artifacts, f"{system_id} has an empty placement — seeding nothing tests nothing"
        assert all(count >= 1 for count in artifacts.values()), (
            f"{system_id} places a zero-count artifact — seeding nothing tests nothing"
        )


def test_every_registered_participant_has_a_cleanup() -> None:
    """V8-13's fix must scale with the registry, not lag it."""
    module = _load()
    source = inspect.getsource(module._cleanup)  # type: ignore[attr-defined]

    missing = [sid for sid in system_ids() if f'"{sid}"' not in source]
    assert not missing, (
        f"_cleanup has no branch for {missing} — their conformance residue would "
        "accumulate in the account, which is the defect V8-13 recorded"
    )


def test_the_no_seeder_escape_hatch_stays_deleted() -> None:
    """The skip string that let six participants pass ungraded must not return."""
    assert "no conformance seeder" not in CONFORMANCE.read_text(encoding="utf-8")


def test_the_subject_fixture_tears_down() -> None:
    """The fixture must yield and then clean — a plain return reintroduces V8-13."""
    text = CONFORMANCE.read_text(encoding="utf-8")
    fixture_start = text.index("def subject(")
    fixture_body = text[fixture_start : text.index("\ndef ", fixture_start)]

    assert "yield handle" in fixture_body
    assert fixture_body.index("yield handle") < fixture_body.rindex("_cleanup("), (
        "cleanup must run after the test, in teardown position"
    )


# ─── the integration suite must not leak either (V9-4) ────────────────────────────────

INTEGRATION = REPO / "tests" / "integration" / "test_saga.py"


def test_the_integration_suite_seeds_inside_its_teardown_guard() -> None:
    """A fixture that fails during SETUP must not leave what it already seeded.

    Seeding walks the systems in order, so a failure on the seventh leaves six
    populated. When that happened for real (V9-2's AttributeError fired on
    notify-suppression, which seeds last), pytest never reached teardown and two
    subjects' data outlived the run across seven services — V8-13's residue problem
    returning through a door left open in a different suite.

    The structural fix is that every seed happens inside a block whose exit tears
    down, so there is no ordering a caller can choose that skips it.
    """
    text = INTEGRATION.read_text(encoding="utf-8")

    assert "def _seeded_subject(" in text, (
        "the integration suite lost its seed-and-teardown context manager — a bare "
        "_seed() call leaves residue whenever it raises partway (V9-4)"
    )
    start = text.index("def _seeded_subject(")
    body = text[start : text.index("\n@pytest.fixture", start)]
    assert "finally:" in body, "teardown must run however the block exits, not only on success"
    # Ordering is the whole claim: `try:` BEFORE `_seed(` BEFORE `finally:`. Checking
    # only "_seed comes before finally" passes for the defective arrangement too —
    # a seed hoisted above the try is still textually earlier — which is a guard that
    # cannot go red, the defect class this file exists to prevent.
    assert body.index("try:") < body.index("_seed(") < body.index("finally:"), (
        "seeding must happen INSIDE the try, not before it — a seed that raises "
        "above the guard leaves everything it already wrote (V9-4)"
    )
    assert "_teardown(" in body.split("finally:")[1]

    # And no test may seed outside it: every _seed call belongs to the guard.
    seeds_outside = [
        line.strip()
        for line in text.splitlines()
        if "_seed(rig" in line and "def " not in line and "_seeded_subject" not in line
    ]
    assert len(seeds_outside) == 1, (
        f"_seed is called outside the teardown guard: {seeds_outside} — every seeded "
        "subject needs an exit path that removes it"
    )
