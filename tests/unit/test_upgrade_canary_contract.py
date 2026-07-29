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
