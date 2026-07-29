"""The Mermaid blocks in ARCHITECTURE.md must match `docs/diagrams/`, and no source may
be an orphan nobody renders.

`docs/VALIDATION.md`'s sweep has listed this as a structural check since the first pass —
*"embedded diagrams in ARCHITECTURE.md remain byte-identical to docs/diagrams/ sources"* —
and nothing enforced it. A claim with a named control and no mechanism is the defect class
that file exists to record, so it should not itself have been one.

**Writing the mechanism corrected the claim.** "Byte-identical" is not the convention the
repo actually follows: each source opens with a `%% Source of truth: ARCHITECTURE.md §N`
provenance line that the embedded copy drops, because inside ARCHITECTURE.md that line
would point at itself. Three diagrams differed by exactly that one line and nothing else.
Encoding the stricter claim would have produced a test that failed on correct files — so
this asserts the real rule, and VALIDATION.md now states it.

The drift it prevents is not hypothetical: `Recheck --> Blocked: new hold found` survived
[ADR-027](../../docs/adr/ADR-027-holds-block-a-scope-not-a-subject.md) making it wrong,
in both copies, because nothing compared either against the behaviour or against the other.

`04-recovery-semantics.mermaid` is deliberately **not** embedded — it is linked from the
README and ADR-002, which VALIDATION.md's first entry records. So a source that appears in
no ```mermaid block is fine, provided something links to it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO / "docs" / "ARCHITECTURE.md"
DIAGRAMS = REPO / "docs" / "diagrams"

_BLOCK = re.compile(r"^```mermaid\r?\n(.*?)^```", re.DOTALL | re.MULTILINE)

#: The one line a source carries and its embedded copy does not. The trailing newline is
#: part of the match: blanking the line instead of removing it leaves an empty line the
#: embedded copy does not have, which fails for a reason that has nothing to do with drift.
_PROVENANCE = re.compile(r"^%% Source of truth:.*\n?", re.MULTILINE)

#: Files that may reference a diagram instead of embedding it.
_LINKING_DOCS = ("README.md", "docs/ROADMAP.md", "docs/adr", "docs/PROJECT-STRUCTURE.md")

_SOURCES = sorted(DIAGRAMS.glob("*.mermaid"))


def _comparable(text: str) -> str:
    """Line endings normalised (the repo is developed on Windows and git converts on
    checkout, so CRLF is not drift a reader can see) and the provenance line removed."""
    return _PROVENANCE.sub("", text.replace("\r\n", "\n")).strip()


def _embedded() -> list[str]:
    body = ARCHITECTURE.read_text("utf-8")
    return [_comparable(match.group(1)) for match in _BLOCK.finditer(body)]


def _sources() -> dict[str, str]:
    return {path.name: _comparable(path.read_text("utf-8")) for path in _SOURCES}


def test_there_are_diagrams_and_blocks_to_compare() -> None:
    """A regex that matched nothing would make every assertion below vacuous — the
    "gate that cannot gate" shape from the baseline validation pass."""
    assert _SOURCES, f"no .mermaid sources in {DIAGRAMS}"
    assert _embedded(), "no ```mermaid blocks in ARCHITECTURE.md — has the fence changed?"


def test_every_embedded_block_has_a_source() -> None:
    """A diagram authored inline has no editable source, so the next person to change it
    edits the rendered copy and `docs/diagrams/` silently stops being the set of
    diagrams."""
    sources = set(_sources().values())
    orphans = [block.splitlines()[0] for block in _embedded() if block not in sources]
    assert not orphans, (
        f"{len(orphans)} embedded Mermaid block(s) match no source in docs/diagrams/: "
        f"{orphans}. Add the source, or fix whichever copy drifted."
    )


@pytest.mark.parametrize("name", [path.name for path in _SOURCES])
def test_a_source_is_either_embedded_verbatim_or_linked(name: str) -> None:
    """Two legitimate states, and no third one.

    Embedded means the copy a reader sees is this file. Linked means the file stands alone
    (`04-recovery-semantics.mermaid`). A source that is neither is a diagram nobody renders
    and nobody would notice going stale.
    """
    if _sources()[name] in _embedded():
        return

    referenced = [
        candidate
        for candidate in _LINKING_DOCS
        for path in (
            [REPO / candidate] if (REPO / candidate).is_file() else (REPO / candidate).rglob("*.md")
        )
        if name in path.read_text("utf-8")
    ]
    assert referenced, (
        f"docs/diagrams/{name} is neither embedded in ARCHITECTURE.md nor linked from "
        f"{list(_LINKING_DOCS)}. Either it drifted from its embedded copy — compare them — "
        f"or it is a diagram with no reader."
    )
