"""Every documented setting must have a mechanism, or say that it does not yet.

`.env.example` is the first file a new user edits, and it is the file with the least
feedback: nothing fails when you set a variable that nothing reads. That is how V4-1
happened — `AWS_REGION` was documented as configuring the deploy and was read by nothing,
so a user could target one region and deploy to another with no signal at all.

The rule this test enforces is deliberately weak enough to be honest: a variable is fine
if the code reads it, **or** if its comment says which milestone will. What is not fine is
a variable that does neither, because a reader cannot tell "not built yet" from "you
typed it wrong" — and neither can the person who added it.

Hermetic: reads files, runs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / ".env.example"

#: Where a consumed variable may legitimately be read from. Docs are excluded on
#: purpose — a variable mentioned only in prose is precisely the defect.
CONSUMERS = ("src", "infra", "Makefile", ".github/workflows/ci.yml")

#: "⏳ lands at M4", "lands at M5 (grace window) and M8", …
_MARKER = re.compile(r"lands at M\d+", re.IGNORECASE)

_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def _variables() -> list[tuple[str, str]]:
    """Each variable in `.env.example` with the comment block immediately above it."""
    found: list[tuple[str, str]] = []
    comment: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comment.append(stripped)
            continue
        match = _ASSIGNMENT.match(stripped)
        if match:
            found.append((match.group(1), "\n".join(comment)))
        if not stripped:
            comment.clear()
    return found


def _sources() -> str:
    chunks: list[str] = []
    for entry in CONSUMERS:
        path = REPO / entry
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix in {".py", ".yml", ".yaml", ".toml"}:
                    chunks.append(child.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(chunks)


SOURCES = _sources()
VARIABLES = _variables()


def test_the_parser_found_something() -> None:
    """A test that scanned an empty list would pass no matter what the file said."""
    names = {name for name, _ in VARIABLES}
    assert len(names) >= 8
    assert "AWS_REGION" in names


@pytest.mark.parametrize(("name", "comment"), VARIABLES, ids=[name for name, _ in VARIABLES])
def test_each_variable_is_consumed_or_marked(name: str, comment: str) -> None:
    if name in SOURCES:
        return
    assert _MARKER.search(comment), (
        f"{name} is documented in .env.example, is read by nothing under {list(CONSUMERS)}, "
        f"and carries no 'lands at Mx' marker. Either wire it up, delete it, or say which "
        f"milestone consumes it — a setting with no mechanism describes an intention "
        f"(VALIDATION V4-1)."
    )


def test_the_stack_prefix_stays_gone() -> None:
    """Stage — not a name prefix — is how two deployments in one account stay apart.

    A configurable prefix reads like isolation and is not: `asdp-` is a literal in the
    ADRs, infra/README.md and the synth assertions, so a second deploy under a different
    prefix would quietly update the first one's stack instead of creating its own.
    """
    assert "PII_ERASURE_STACK_PREFIX" not in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_required_variables_are_actually_live() -> None:
    """The two the deploy path depends on today. If either falls out of the Makefile,
    `make deploy-dev` goes back to guessing."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "AWS_REGION" in makefile
    assert "PII_ERASURE_STAGE" in makefile
