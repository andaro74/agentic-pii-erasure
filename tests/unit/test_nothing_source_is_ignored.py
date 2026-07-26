"""No source file may be silently excluded from the repository.

This exists because of V6-1: `.gitignore` carried the stock Python template's unanchored
`MANIFEST` pattern, meant for setuptools' generated file at the repo root. On a
case-insensitive filesystem git matched it against the **directory**
`src/pii_erasure/manifest/`, so an entire package — the manifest models, the digest, KMS
signing, validation — was excluded from a commit without a word from `git add -A`.

The failure was invisible locally (the files exist) and *misreported* in CI: `make check`
runs lint before tests, so the missing package surfaced as an import-sorting complaint
rather than as "module not found". A defect that lies about what it is costs more than
one that fails loudly, which is what earns this test its place in the hermetic gate.

Ignoring build output is fine and expected — the check is scoped to the directories that
hold hand-written source.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Directories whose contents are authored, not generated. `infra/build` and
#: `infra/cdk.out` are deliberately excluded: they are staged artefacts and *should* be
#: ignored (bar the committed asset marker, which has its own test).
SOURCE_ROOTS = ("src", "tests", "infra/stacks", "evals", "seeds", "policies")

SOURCE_SUFFIXES = {".py", ".json", ".cedar", ".yaml", ".yml", ".toml"}

GIT = shutil.which("git")


def _source_files() -> list[Path]:
    found: list[Path] = []
    for root in SOURCE_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue  # lands at a later milestone
        found.extend(
            path
            for path in base.rglob("*")
            if path.is_file() and path.suffix in SOURCE_SUFFIXES and "__pycache__" not in path.parts
        )
    return found


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_no_source_file_is_gitignored() -> None:
    files = _source_files()
    assert len(files) > 30, (
        "the file walk found almost nothing — the test is looking in the wrong place"
    )

    # `git check-ignore --stdin` echoes only the paths that ARE ignored, so an empty
    # stdout is the passing state and the failure message names every offender.
    result = subprocess.run(
        [str(GIT), "check-ignore", "--stdin"],
        cwd=REPO,
        input="\n".join(str(path.relative_to(REPO)) for path in files),
        capture_output=True,
        text=True,
    )
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "these authored files are excluded from the repository by .gitignore, so a clone "
        "would not contain them:\n  " + "\n  ".join(ignored)
    )


@pytest.mark.skipif(GIT is None, reason="git is not on PATH")
def test_every_source_package_has_a_tracked_init() -> None:
    """A package whose `__init__.py` is untracked is a package that does not exist for
    anyone but you — the exact shape V6-1 took."""
    result = subprocess.run(
        [str(GIT), "ls-files", "src"], cwd=REPO, capture_output=True, text=True, check=True
    )
    tracked = set(result.stdout.split())

    for init in (REPO / "src").rglob("__init__.py"):
        if "__pycache__" in init.parts:
            continue
        relative = init.relative_to(REPO).as_posix()
        assert relative in tracked, f"{relative} exists on disk but is not tracked by git"
