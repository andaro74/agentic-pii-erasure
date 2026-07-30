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


def test_the_lock_target_pins_its_own_tooling() -> None:
    """`make lock` is the mechanism invariant 9 names, and it was the least pinned thing
    in the repo.

    `pip install -q pip-tools`, unpinned, against whatever pip the venv had. pip-tools
    imports `pip._internal.utils.compat.stdlib_pkgs`, a private symbol pip 26 removed, so
    the target failed for whoever had upgraded pip and worked for everyone else — and it
    is called by exactly one thing, the release canary, so nothing noticed until that ran
    (V12-7).

    Both tools pinned, in a throwaway venv: locking must not depend on which pip a
    developer has, and must not mutate the project venv's pip as a side effect.
    """
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    tools = re.search(r"^LOCK_TOOLS\s*:=\s*(.+)$", makefile, re.MULTILINE)
    assert tools, "LOCK_TOOLS is gone — `make lock` is installing its tooling unpinned again"
    assert re.search(r'"pip<\d|"pip==\d', tools.group(1)), (
        "the pip version is unconstrained; pip 26 removed the private symbol pip-tools "
        "imports, so this is the constraint that matters"
    )
    assert re.search(r'"pip-tools==\d', tools.group(1)), "pip-tools is not pinned exactly"

    lock_target = makefile[makefile.index("\nlock:") :]
    lock_target = lock_target[: lock_target.index("\n.PHONY")]
    assert "$(LOCK_ENV_BIN)/python -m piptools" in lock_target, (
        "the compile step no longer runs from the isolated venv — regenerating a lockfile "
        "must not change the project venv"
    )
    assert lock_target.count("rm -rf $(LOCK_ENV)") == 2, (
        "the tool venv must be removed before and after: it is a tool, not an environment"
    )


def test_the_installed_versions_match_the_pins() -> None:
    """The environment running these tests is the claim's third leg."""
    from importlib.metadata import version

    for name, pinned in _pyproject_pins().items():
        assert version(name) == pinned, (
            f"installed {name} {version(name)} != pinned {pinned} — reinstall before "
            "trusting any checkpoint-shaped test result"
        )
