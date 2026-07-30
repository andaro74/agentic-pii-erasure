"""The canary script is the contract; the test implements it. This checks they agree.

ROADMAP M9's trap says it plainly: *"the script is the contract; the test implements it
exactly."* Two files stating one protocol is exactly the shape that drifted at ADR-027
(a hold rule in two packages) and at V11-4 (ledger event names restated by hand). So the
agreement is asserted rather than maintained by care.

The canary itself needs a deployed stack and a real upgrade, so it cannot run here. What
CAN run here is everything about the contract that is text: the stage names, both pins
being named, and the script refusing to bump one pin without the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "upgrade_canary.sh"
TEST = REPO / "tests" / "integration" / "test_upgrade_canary.py"


def test_the_script_exists() -> None:
    """The Makefile shells out to it, and ROADMAP calls it the contract."""
    assert SCRIPT.is_file(), f"{SCRIPT} is referenced by the Makefile and ROADMAP M9"


def test_the_stages_match() -> None:
    """`CANARY_STAGE=pause|resume` appears in the script header; the test enumerates the
    same two. A third stage in one and not the other is a half-run canary."""
    from tests.integration.test_upgrade_canary import _STAGES

    header = SCRIPT.read_text(encoding="utf-8")
    documented = set(re.findall(r"CANARY_STAGE=(\w+)", header))
    assert documented == set(_STAGES), f"script documents {documented}, test has {set(_STAGES)}"


def test_the_test_has_no_default_stage() -> None:
    """A canary that silently picked a stage would report success for whichever half
    ran — and both halves passing separately is the entire point."""
    source = TEST.read_text(encoding="utf-8")
    assert 'os.environ.get("CANARY_STAGE", "")' in source, (
        "CANARY_STAGE must default to empty and fail loudly, never to a stage"
    )


@pytest.mark.parametrize("pin", ["langgraph", "langgraph-checkpoint-aws"])
def test_the_script_names_both_pins(pin: str) -> None:
    """Serialization lives in the checkpoint package as much as in langgraph itself
    (invariant 9). Canarying one while bumping the other is VALIDATION baseline finding
    #3 wearing new clothes — a pin protecting the wrong layer."""
    assert pin in SCRIPT.read_text(encoding="utf-8")


def test_the_script_refuses_one_pin_without_the_other() -> None:
    """The pins move in lockstep or not at all. A canary that accepted a single version
    would let someone bump langgraph and prove nothing about the serializer."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "both pins move together" in source
    assert "invariant 9" in source


def test_the_summary_keys_off_whether_a_pin_actually_moved() -> None:
    """The two packages release independently, so "target equals current" is a normal,
    honest case — `langgraph-checkpoint-aws` had no newer release than its pin when this
    was written. What must never happen is a run reporting a canaried upgrade for a pin
    that never moved, which is what keying the summary off *argument presence* did.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'if [ "$MOVED_ANY" = no ]' in source, (
        "the summary must branch on whether a pin moved, not on whether one was passed"
    )
    assert "already current" in source, "a pin with no newer release must be labelled as such"


def test_the_hermetic_gate_runs_on_the_new_versions_before_the_deploy() -> None:
    """A new version that breaks an API we call should cost three unit failures, not a
    deploy and then a mysterious resume failure. Order is the whole assertion."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    check = next(i for i, line in enumerate(lines) if line.strip() == "make check")
    deploy = next(i for i, line in enumerate(lines) if line.strip() == "make deploy-dev")
    assert check < deploy, "the hermetic gate must run before the deploy it would invalidate"


#: Where an exact `langgraph` pin lives. Discovered by reading, not asserted from memory —
#: the point of the test below is that this list is derived from the tree.
_PIN_BEARING = ("pyproject.toml", "Makefile")


def test_the_canary_bumps_every_file_that_names_a_pin() -> None:
    """The canary bumped two of the three places a pin lives, so it could never pass.

    `SAGA_PINS` in the Makefile is what `make package` installs into the Lambda asset —
    deliberately a separate string, so a reader can see what ships. The canary rewrote
    `pyproject.toml` and `requirements.lock` and left it, which does not quietly ship the
    old version: `make lock` writes the new one into the lockfile, `make package` passes
    that as a constraint, and pip becomes unresolvable. Unpassable either way (V12-6).

    Derived from the tree rather than hardcoded, so a **fourth** location — a Dockerfile,
    a second asset target, a constraints file — fails here instead of at the next canary.
    """
    pin = re.compile(r'"langgraph(?:-checkpoint-aws)?==\d')
    found = sorted(
        path.name
        for path in REPO.iterdir()
        if path.is_file()
        and path.suffix in ("", ".toml", ".lock", ".cfg", ".txt")
        and path.name != "requirements.lock"
        and pin.search(path.read_text(encoding="utf-8", errors="ignore"))
    )
    assert found == sorted(_PIN_BEARING), (
        f"the set of files naming an exact langgraph pin changed: {found}. Teach "
        f"scripts/upgrade_canary.sh to bump the new one, add it to _PIN_BEARING, and "
        f"check `restore()` puts it back on failure."
    )

    script = SCRIPT.read_text(encoding="utf-8")
    for name in found:
        assert name in script, (
            f"{name} pins langgraph but scripts/upgrade_canary.sh never rewrites it — the "
            f"canary would test a version the deployed Lambda does not run"
        )


def test_a_failed_canary_restores_every_file_it_edits() -> None:
    """`restore()` runs on any non-zero exit. A file it edits but does not restore leaves
    the tree claiming a version that was never canaried — and the failure path is exactly
    when nobody is inclined to check."""
    script = SCRIPT.read_text(encoding="utf-8")
    restore = script[script.index("restore() {") : script.index("trap ")]
    for name in _PIN_BEARING:
        assert name in restore, f"restore() does not put {name} back after a failure"
    # The venv is deliberately left on the new versions, which makes the environment
    # disagree with the files it just restored. `make check` then fails on
    # test_the_installed_versions_match_the_pins — correctly, and confusingly unless the
    # script says so. Deliberate and stated, or it is just a mess someone else inherits.
    assert "make install" in restore, (
        "restore() leaves the venv on the target versions but does not tell the operator "
        "how to get back — the next `make check` fails for a reason unrelated to their work"
    )


def test_the_pins_are_still_exact() -> None:
    """The thing the canary protects. `>=` here would mean a `pip install` could move
    the serializer under a paused saga with no canary run at all."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for pin in ("langgraph", "langgraph-checkpoint-aws"):
        assert re.search(rf'^  "{re.escape(pin)}==\d', pyproject, re.MULTILINE), (
            f"{pin} is no longer pinned to an exact version (invariant 9)"
        )


def test_the_state_file_is_ignored_by_git() -> None:
    """`.canary-state.json` carries a live thread id between two processes. Committing
    one would make a later canary resume a thread from someone else's stack."""
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".canary-state.json" in ignored
