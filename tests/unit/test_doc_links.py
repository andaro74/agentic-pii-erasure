"""Every relative link in the docs points at something that exists.

`docs/VALIDATION.md` lists "all relative markdown links resolve" as sweep item 3 of the
validation discipline — and it has been a *manual* sweep, run when someone remembers to
run it. This makes it executable, for the same reason `test_adr_index.py` exists: the ADR
set is the reason this repo is worth reading, and a cross-reference that quietly stops
resolving degrades it one link at a time without breaking anything that runs.

The immediate provocation was ADR-025. It changed the Runtime's artifact from a container
to an S3 code zip and named what it superseded — ARCHITECTURE §4 and PROJECT-STRUCTURE's
`runtime/Dockerfile` — but ADR-015's "Cost 2 — two deployment artifacts" still promised an
ECR image, and nothing noticed. **This test would not have caught that**, and saying so is
the point: a link to a live file that makes a stale *claim* resolves perfectly. What this
catches is the mechanical half — the renamed file, the moved doc, the ADR referenced by a
number nobody wrote. That half is worth automating precisely because the other half never
can be, and a reader who finds one dead link stops trusting the other four hundred.

Anchors are checked too, against GitHub's heading-slug rules — but the docs currently
contain exactly **one** anchored link, so that half is close to vacuous on today's corpus
and it would be dishonest to present it otherwise. It is here because a heading rename
leaves an anchored link *looking* fine in a diff, and because ARCHITECTURE's §-numbered
prose is the obvious place for more of them. `test_the_slugifier_matches_github` pins the
slug rule directly, so the rule is exercised whether or not any link happens to use it.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parents[2]

#: `[text](target)`, skipping image embeds and reference-style definitions. The nested
#: bracket class handles link text that itself contains brackets, e.g. `[see [1]](x.md)`.
_LINK = re.compile(r"(?<!!)\[(?:[^\[\]]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: Everything the filesystem cannot answer for.
_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "ftp://")


#: Directory names holding code this repo did not write. `build/` and the cloud
#: assemblies stage the Lambda and Runtime assets — thousands of vendored READMEs whose
#: links are their authors' problem, not ours.
_NOT_OURS = {"build", ".venv", "node_modules", ".git", "__pycache__"}


def _is_generated(part: str) -> bool:
    """True for a directory this repo generates rather than authors.

    Cloud assemblies are matched by PREFIX, not by exact name. `cdk.out` was listed
    literally, so when the deploy targets got their own `cdk.out.deploy` (V13-10) the
    scan walked straight into it and graded vendored markdown inside a staged asset.
    An exclusion list keyed on exact names is a list that the next generated directory
    escapes — and this one escaped it within a day.
    """
    return part in _NOT_OURS or part.startswith("cdk.out")


def _markdown_files() -> list[Path]:
    files = [
        path for path in REPO.rglob("*.md") if not any(_is_generated(part) for part in path.parts)
    ]
    assert len(files) > 20, "the markdown scan found almost nothing — wrong directory?"
    return files


def _slug(heading: str) -> str:
    """GitHub's anchor rule: strip formatting, lowercase, spaces to hyphens.

    Deliberately a subset — it handles what this repo's headings actually contain
    (inline code, links, emphasis, `§`, em dashes) rather than every case GitHub does.
    A heading shape it gets wrong would show up as a false failure on a link that works,
    which is the failure mode to avoid, so `test_the_slugifier_matches_github` pins the
    cases in use.
    """
    text = heading.strip()
    text = re.sub(r"`([^`]*)`", r"\1", text)  # inline code
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = re.sub(r"[*_~]", "", text)  # emphasis
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)  # punctuation vanishes
    # One hyphen per space, NOT per run. `github-slugger` substitutes character by
    # character, so a heading with an em dash — which this repo's headings are full of —
    # leaves the space either side and slugifies to a *double* hyphen. Collapsing runs
    # here would be the tidier rule and would quietly bless links GitHub 404s.
    return text.strip().replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """Heading slugs, plus any explicit HTML anchor the doc plants."""
    text = path.read_text(encoding="utf-8")
    slugs = {_slug(match) for match in re.findall(r"^#{1,6}\s+(.*)$", text, re.MULTILINE)}
    slugs |= set(re.findall(r"<a\s+(?:id|name)=[\"']([^\"']+)[\"']", text))
    slugs.discard("")
    return slugs


def _links(path: Path) -> list[str]:
    """Links in `path`, minus the ones inside fenced code blocks.

    A fenced block can contain an illustrative path — a directory tree, a `make` recipe —
    that is not a link and is not required to exist.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    prose, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            prose.append(line)
    return [match.group(1) for match in _LINK.finditer("\n".join(prose))]


def _relative_links() -> list[tuple[Path, str]]:
    found = []
    for path in _markdown_files():
        for target in _links(path):
            if target.startswith(_EXTERNAL) or target.startswith("#"):
                continue
            found.append((path, target))
    assert len(found) > 50, "no relative links extracted — the regex broke, not the docs"
    return found


_RELATIVE_LINKS = _relative_links()


@pytest.mark.parametrize(
    ("source", "target"),
    _RELATIVE_LINKS,
    ids=[f"{s.relative_to(REPO).as_posix()}->{t}" for s, t in _RELATIVE_LINKS],
)
def test_the_link_target_exists(source: Path, target: str) -> None:
    path_part = unquote(target.split("#", 1)[0])
    if not path_part:  # a bare `#anchor`, already filtered
        return
    resolved = (source.parent / path_part).resolve()
    assert resolved.exists(), (
        f"{source.relative_to(REPO).as_posix()} links to {target!r}, which does not exist"
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [(s, t) for s, t in _RELATIVE_LINKS if "#" in t],
    ids=[f"{s.relative_to(REPO).as_posix()}->{t}" for s, t in _RELATIVE_LINKS if "#" in t],
)
def test_the_link_anchor_exists(source: Path, target: str) -> None:
    path_part, anchor = target.split("#", 1)
    resolved = (source.parent / unquote(path_part)).resolve() if path_part else source
    if resolved.suffix != ".md" or not resolved.is_file():
        return  # a line anchor on a source file, e.g. `foo.py#L42`
    assert unquote(anchor).lower() in _anchors(resolved), (
        f"{source.relative_to(REPO).as_posix()} links to {target!r}, but "
        f"{resolved.name} has no such heading"
    )


def test_the_slugifier_matches_github() -> None:
    """The shapes this repo's headings actually take.

    Pinned because a slugifier that is subtly wrong fails *working* links, and the first
    instinct on a false failure is to delete the test rather than fix the rule.
    """
    assert _slug("## §4.2 The reasoning plane") == "42-the-reasoning-plane"
    assert _slug("Invariant 8 — recall gates the build") == "invariant-8--recall-gates-the-build"
    assert _slug("`canonical.py` and its fixtures") == "canonicalpy-and-its-fixtures"
    assert _slug("**Bold** and _italic_") == "bold-and-italic"


def test_a_broken_link_would_actually_fail() -> None:
    """The guard against a vacuous gate.

    `test_the_link_target_exists` is parameterised over links discovered at import time.
    If the extraction silently returned nothing the suite would still be green, which is
    the shape of every gate this repo has caught failing to gate (VALIDATION.md).
    """
    fake = REPO / "docs" / "ARCHITECTURE.md"
    resolved = (fake.parent / "ADR-999-does-not-exist.md").resolve()
    assert not resolved.exists()
    assert _LINK.findall("see [the ADR](adr/ADR-999.md) for more") == ["adr/ADR-999.md"]
