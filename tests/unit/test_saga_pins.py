"""The saga asset's framework pins must equal pyproject's, verbatim (invariant 9).

Two places name the langgraph pins: pyproject.toml (what the tests run against) and
the Makefile's SAGA_PINS (what `make package` ships to Lambda). If they diverge, the
deployed Lambda deserializes checkpoints with a version the test suite never saw —
the silent-stranding failure ADR-016 exists to prevent. This test makes the drift a
`make check` failure instead of a production mystery.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PINNED = ("langgraph", "langgraph-checkpoint-aws")


def _pyproject_pins() -> dict[str, str]:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    pins: dict[str, str] = {}
    for name in _PINNED:
        match = re.search(rf'"{re.escape(name)}==([^"]+)"', text)
        assert match, f"pyproject.toml no longer pins {name} exactly — invariant 9"
        pins[name] = match.group(1)
    return pins


def _makefile_pins() -> dict[str, str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    match = re.search(r"^SAGA_PINS\s*:=\s*(.+)$", text, re.MULTILINE)
    assert match, "Makefile lost its SAGA_PINS line — the saga asset would ship unpinned"
    pins: dict[str, str] = {}
    for token in re.findall(r'"([^"]+)"', match.group(1)):
        name, _, version = token.partition("==")
        pins[name] = version
    return pins


def test_the_makefile_ships_exactly_the_pyproject_pins() -> None:
    assert _makefile_pins() == _pyproject_pins(), (
        "SAGA_PINS and pyproject.toml disagree — bump both together, and only after "
        "make upgrade-canary passes (ADR-016)"
    )


def test_the_installed_versions_match_the_pins() -> None:
    """The environment running these tests is the claim's third leg."""
    from importlib.metadata import version

    for name, pinned in _pyproject_pins().items():
        assert version(name) == pinned, (
            f"installed {name} {version(name)} != pinned {pinned} — reinstall before "
            "trusting any checkpoint-shaped test result"
        )
