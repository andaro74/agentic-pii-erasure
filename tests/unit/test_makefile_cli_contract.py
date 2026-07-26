"""Every CLI invocation in the Makefile must be one the CLI accepts.

This exists because `make seed` failed with `No such option: --tenant`. The `seed` command
was rewritten at M4 and the Makefile target that calls it was not re-read, so the two
drifted apart in the same commit that built them. Nothing caught it: `make check` runs the
*test suite*, never the make targets, and the CLI's own tests call functions directly.

The gap is narrow and worth closing precisely because the failure is so cheap to prevent
and so expensive to hit — `seed` is a deployed-gate target, so the error surfaces only
after a human has stood up a stack and is waiting on it.

Options are introspected from the click command that Typer builds, so this cannot drift
from the real parser the way a hand-maintained list of flag names would.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer.main

from pii_erasure.cli.main import app

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"

#: `$(PY) -m pii_erasure.cli.main <command> [args...]` up to the end of the line.
_INVOCATION = re.compile(r"pii_erasure\.cli\.main\s+([a-z-]+)([^\n]*)")

#: Long options only. Short flags and shell interpolations are not the failure mode here,
#: and pretending to parse `"$${VAR:-default}"` would make this test lie about its reach.
_OPTION = re.compile(r"(--[a-z][a-z0-9-]*)")


def _invocations() -> list[tuple[str, str, list[str]]]:
    text = MAKEFILE.read_text(encoding="utf-8")
    found = [
        (command, tail.strip(), _OPTION.findall(tail))
        for command, tail in _INVOCATION.findall(text)
    ]
    assert found, "no CLI invocations found in the Makefile — the pattern is broken"
    return found


def _click_commands() -> dict[str, object]:
    group = typer.main.get_command(app)
    return dict(getattr(group, "commands", {}))


@pytest.mark.parametrize(
    ("command", "tail", "options"),
    _invocations(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_makefile_calls_a_command_that_exists(
    command: str, tail: str, options: list[str]
) -> None:
    commands = _click_commands()
    assert command in commands, (
        f"the Makefile runs `erasure {command}`, which the CLI does not define. "
        f"Available: {sorted(commands)}"
    )


@pytest.mark.parametrize(
    ("command", "tail", "options"),
    _invocations(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_option_the_makefile_passes_is_accepted(
    command: str, tail: str, options: list[str]
) -> None:
    """The exact failure: `make seed` passed `--tenant` to a command without it."""
    click_command = _click_commands().get(command)
    if click_command is None:
        pytest.skip("covered by the command-exists test above")

    if _is_passthrough(click_command):
        # An unbuilt command swallows unknown flags so it can print "lands at Mx" rather
        # than a parser error. Nothing to check: the invocation cannot fail on options.
        return

    accepted = {
        opt
        for param in getattr(click_command, "params", [])
        for opt in getattr(param, "opts", [])
        if opt.startswith("--")
    }
    unknown = [option for option in options if option not in accepted]
    assert not unknown, (
        f"`make` calls `erasure {command} {tail}` but {unknown} is not an option of that "
        f"command. Accepted: {sorted(accepted)}"
    )


def _is_passthrough(click_command: object) -> bool:
    settings = getattr(click_command, "context_settings", None) or {}
    return bool(settings.get("ignore_unknown_options"))


def test_only_unbuilt_commands_swallow_unknown_options() -> None:
    """Passthrough is a milestone-gating device, never a way to dodge the check above.

    A *built* command with `ignore_unknown_options` would accept `--typo` in silence and
    do the wrong thing — which is worse than the parser error this whole file exists to
    prevent. So the exemption is confined to commands that do nothing but announce their
    milestone, and it disappears automatically when they are implemented.
    """
    from pii_erasure.cli.main import _UNBUILT

    leaked = sorted(
        name
        for name, click_command in _click_commands().items()
        if _is_passthrough(click_command) and name not in _UNBUILT
    )
    assert not leaked, (
        f"{leaked} accept unknown options but are no longer unbuilt — remove "
        "context_settings now that they have a real signature"
    )
