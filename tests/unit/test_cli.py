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
