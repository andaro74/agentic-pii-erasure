"""M0 CLI contract: help and version are real; every unbuilt command exits
non-zero with its landing milestone. A stub that exits 0 is baseline finding #2
(a gate that couldn't gate) wearing a CLI."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pii_erasure import __version__
from pii_erasure.cli.main import _UNBUILT, app

runner = CliRunner()


def test_help_shows_the_app() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "erasure" in result.output


def test_version_matches_package_metadata() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


@pytest.mark.parametrize("command", sorted(_UNBUILT))
def test_unbuilt_commands_exit_nonzero_and_name_their_milestone(command: str) -> None:
    result = runner.invoke(app, [command])
    assert result.exit_code == 1, f"`erasure {command}` must not pretend success"
    combined = result.output + (result.stderr or "")
    assert _UNBUILT[command] in combined
    assert "ROADMAP" in combined


def test_the_unbuilt_table_is_empty_at_m8() -> None:
    """`_UNBUILT` emptied at M8 — the module docstring said it would.

    This test exists because the parametrised one above now has **nothing to iterate**
    and skips. A suite where the only check of a mechanism silently stops running is the
    vacuous-gate shape this repo keeps catching, so the emptiness is asserted directly
    rather than left to be inferred from a skip nobody reads.
    """
    assert _UNBUILT == {}, f"still unbuilt: {sorted(_UNBUILT)} — update docs/ROADMAP.md too"


def test_the_unbuilt_mechanism_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table is empty; the machinery must not rot.

    The next command added ahead of its milestone needs this to still exit non-zero, and
    "it worked when we last had an entry" is not a property — it is a memory.
    """
    import typer

    from pii_erasure.cli import main

    monkeypatch.setitem(main._UNBUILT, "hypothetical", "M99")
    with pytest.raises(typer.Exit) as raised:
        main._unbuilt("hypothetical")
    assert raised.value.exit_code == 1


@pytest.mark.parametrize(
    "command", ["ledger", "discover", "walkthrough", "threads", "resume", "approve"]
)
def test_every_operator_command_is_registered(command: str) -> None:
    """The six commands M8 promised. Named verbatim rather than read from the app, so
    deleting one is a test failure instead of a quietly shorter list."""
    import typer.main

    group = typer.main.get_command(app)
    assert command in getattr(group, "commands", {})
