"""Every ADR appears in every index that claims to list them all.

This exists because ADR-023 was written, linked from six documents, and left out of both
index tables — `docs/adr/README.md` and ARCHITECTURE §17. Neither omission breaks anything
that runs, which is exactly why it survives: the ADR set is the reason this repo is worth
reading, and an index that silently stops being complete degrades it one decision at a
time.

The check is deliberately dumb. It does not verify that an entry is *accurate* — no test
can — only that the decision is not missing from a list that presents itself as exhaustive.
That is the failure mode that actually happens, because writing the ADR feels like the
work and updating two tables does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADR_DIR = REPO / "docs" / "adr"


def _adr_numbers() -> list[str]:
    numbers = sorted(
        match.group(1)
        for path in ADR_DIR.glob("ADR-*.md")
        if (match := re.match(r"ADR-(\d{3})-", path.name))
    )
    assert len(numbers) > 15, "the ADR scan found almost nothing — wrong directory?"
    return numbers


@pytest.mark.parametrize("number", _adr_numbers())
def test_the_adr_is_listed_in_the_adr_readme(number: str) -> None:
    """`docs/adr/README.md` carries the canonical table, superseded entries included."""
    text = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"\[{number}\]\(ADR-{number}-", text), (
        f"ADR-{number} exists but is absent from docs/adr/README.md's table"
    )


@pytest.mark.parametrize("number", _adr_numbers())
def test_the_adr_is_listed_in_the_architecture_summary(number: str) -> None:
    """ARCHITECTURE §17 summarises every decision with its rejected alternatives.

    Matches a leading table cell — plain (`| 023 |`) or struck through for a superseded
    decision (`| ~~012~~ |`) — rather than the number anywhere in the prose, which would
    pass on an incidental mention. Both the zero-padded and bare forms are accepted, since
    the table's own convention is what it is rather than what this test would prefer.
    """
    text = (REPO / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    forms = {number, number.lstrip("0") or "0"}
    pattern = "|".join(sorted(forms))
    assert re.search(rf"^\|\s*(~~)?({pattern})(~~)?\s*\|", text, re.MULTILINE), (
        f"ADR-{number} exists but has no row in ARCHITECTURE's decision table"
    )


def test_every_indexed_adr_actually_exists() -> None:
    """The other direction: an index entry pointing at a file nobody wrote.

    Cheap, and it closes the loop — without it the pair of tests above would be satisfied
    by an index that lists phantom decisions.
    """
    text = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\((ADR-\d{3}-[a-z0-9-]+\.md)\)", text))
    assert linked, "no ADR links found — the extraction is broken, not the index"

    missing = sorted(name for name in linked if not (ADR_DIR / name).is_file())
    assert not missing, f"docs/adr/README.md links to ADRs that do not exist: {missing}"
