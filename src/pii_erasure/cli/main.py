"""Typer entrypoint.

M0 walking skeleton: `--help` and `version` are real; every command whose
milestone has not landed prints "⏳ lands at Mx" and EXITS NON-ZERO. A stub that
pretends success is the defect class docs/VALIDATION.md exists to catch —
baseline finding #2 was a gate that exited 0 while gating nothing.

Real implementations land per docs/ROADMAP.md and replace entries in
_UNBUILT one milestone at a time; the table shrinking to empty is M8.
"""

from __future__ import annotations

import typer
from rich.console import Console

from pii_erasure import __version__
from pii_erasure.observability.logging import configure_logging

app = typer.Typer(
    name="erasure",
    help="Agentic PII erasure — the agent proposes, the saga disposes. AWS-only (ADR-017).",
    no_args_is_help=True,
)
_console = Console(stderr=True)

# command name -> milestone that makes it real (docs/ROADMAP.md)
_UNBUILT: dict[str, str] = {
    "seed": "M4",
    "inspect": "M4",
    "ledger": "M5",
    "discover": "M7",
    "walkthrough": "M8",
    "threads": "M8",
    "resume": "M8",
    "approve": "M8",
}


@app.callback()
def _init() -> None:
    configure_logging()


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


def _unbuilt(command: str) -> None:
    milestone = _UNBUILT[command]
    _console.print(f"⏳ `erasure {command}` lands at {milestone} — docs/ROADMAP.md")
    raise typer.Exit(code=1)


@app.command()
def seed() -> None:
    """Write fabricated subjects into the deployed participants. (M4)"""
    _unbuilt("seed")


@app.command()
def inspect() -> None:
    """Dump one participant's state. (M4)"""
    _unbuilt("inspect")


@app.command()
def ledger() -> None:
    """Print the hash-chained audit ledger and verify the chain. (M5)"""
    _unbuilt("ledger")


@app.command()
def discover() -> None:
    """Run discovery for one subject against the deployed stack. (M7)"""
    _unbuilt("discover")


@app.command()
def walkthrough() -> None:
    """The full arc: discover → soft delete → pause → hard delete → certificate. (M8)"""
    _unbuilt("walkthrough")


@app.command()
def threads() -> None:
    """List checkpoint threads and their paused state. (M8)"""
    _unbuilt("threads")


@app.command()
def resume() -> None:
    """Manually resume a paused saga. (M8)"""
    _unbuilt("resume")


@app.command()
def approve() -> None:
    """Approve or deny a paused saga. (M8)"""
    _unbuilt("approve")


if __name__ == "__main__":
    app()
