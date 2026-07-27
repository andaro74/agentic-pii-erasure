"""A Lambda asset must be a function of the SOURCE, not of the build machine (V9-1).

The deployed-code staleness preflight (V7-2) compares the CDK asset hash of the
working tree against the deployed stack's. That comparison is only meaningful if
packaging the same source twice produces the same bytes. It did not: `pip install
--target` materialises entry-point wrappers into `bin/` even under `--platform`, and
those wrappers are the two worst possible things to hash —

* Windows `.exe` launchers are a zip with a stub prepended, and the zip carries an
  embedded timestamp, so the bytes differ on **every build**;
* POSIX console scripts embed the **build machine's** interpreter path in a shebang,
  so the bytes differ on every *machine*.

Either one turns the staleness check from a control into a false alarm that fires on
every run — the exact mirror of V7-2, where the control was silent when it should
have fired. A gate that cannot pass is not a strict gate; it is a broken one.

Nothing in Lambda ever runs a console script, so the fix is to strip `bin/` in `make
package`. These tests assert both halves of that: the recipe still does it, and the
staged assets (when staged) actually carry none of the offending artifacts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"


def _asset_dirs() -> tuple[Path, ...]:
    """Every asset the Makefile stages, read FROM the Makefile.

    Hardcoding this list is what let V10-5 through twice over: the Makefile had three
    hand-maintained cleanup lists and this file had a fourth, and the discovery Runtime
    asset made it into two of the four. Deriving it means a fifth asset is covered the
    moment it is declared, rather than when someone remembers this file exists.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    declared = dict(re.findall(r"^(\w+_ASSET) := (\S+)$", text, re.MULTILINE))
    assert declared, "no *_ASSET variables found — the Makefile parse is wrong"
    return tuple(REPO / path for path in sorted(declared.values()))


ASSETS = _asset_dirs()

#: Extensions that are host-specific by construction. `.pyc` is here for the same
#: reason as `bin/`: bytecode embeds a source path and a timestamp.
_HOST_SPECIFIC_SUFFIXES = (".exe", ".pyc", ".pyo")


def _staged(asset: Path) -> bool:
    """True when `make package` has actually run — the dir ships empty but tracked."""
    return asset.is_dir() and any(p.name != ".gitkeep" for p in asset.iterdir())


def test_the_package_recipe_strips_console_scripts() -> None:
    """Always meaningful, including in CI where nothing is staged."""
    recipe = MAKEFILE.read_text(encoding="utf-8")
    assert "$(addsuffix /bin,$(ASSETS))" in recipe, (
        "make package no longer strips bin/ from every asset — the asset hash becomes "
        "a function of the build machine and the staleness preflight cries wolf (V9-1)"
    )
    assert "-name RECORD" in recipe, (
        "the RECORD filter is gone — dist-info would still list the stripped scripts, "
        "and their hashes are exactly the non-deterministic bytes"
    )


@pytest.mark.parametrize("asset", ASSETS, ids=lambda path: path.name)
def test_a_staged_asset_carries_no_console_scripts(asset: Path) -> None:
    if not _staged(asset):
        pytest.skip(f"{asset.name} is not staged — run `make package` to check it")
    assert not (asset / "bin").exists(), (
        f"{asset.name} still has bin/ — console-script wrappers are host-specific and "
        "non-deterministic, and nothing in Lambda runs them (V9-1)"
    )


@pytest.mark.parametrize("asset", ASSETS, ids=lambda path: path.name)
def test_a_staged_asset_carries_no_host_specific_binaries(asset: Path) -> None:
    if not _staged(asset):
        pytest.skip(f"{asset.name} is not staged — run `make package` to check it")
    offending = [
        str(path.relative_to(asset))
        for path in asset.rglob("*")
        if path.is_file() and path.suffix.lower() in _HOST_SPECIFIC_SUFFIXES
    ]
    assert not offending, (
        f"{asset.name} carries host-specific files {offending[:5]} — these change per "
        "machine or per build, so the asset hash stops identifying the source (V9-1)"
    )


@pytest.mark.parametrize("asset", ASSETS, ids=lambda path: path.name)
def test_record_metadata_does_not_reference_stripped_scripts(asset: Path) -> None:
    """Honesty, not just determinism: dist-info must describe what actually shipped."""
    if not _staged(asset):
        pytest.skip(f"{asset.name} is not staged — run `make package` to check it")
    lying = [
        str(record.relative_to(asset))
        for record in asset.rglob("RECORD")
        for line in record.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith("..") and "/bin/" in line.split(",")[0]
    ]
    assert not lying, f"{asset.name}: RECORD still lists stripped console scripts {lying[:3]}"


def test_every_declared_asset_is_in_the_cleanup_list() -> None:
    """V10-5's actual root cause, guarded.

    `make deploy-dev` failed with *"Your artifact contains Python cache files that are
    incompatible with the target runtime"* — AgentCore Runtime REFUSES an artifact
    carrying `__pycache__`, because x86 Windows bytecode does not run on arm64 Linux
    and pip byte-compiles on install by default. The strip existed; it just named two
    of the three assets.

    For a Lambda asset stray bytecode was merely non-deterministic (V9-1). For the
    Runtime it is a hard deploy failure. Same defect, and the difference in
    consequence is the argument for deriving the list instead of typing it.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    declared = dict(re.findall(r"^(\w+_ASSET) := (\S+)$", text, re.MULTILINE))
    listed = re.search(r"^ASSETS := (.+)$", text, re.MULTILINE)
    assert listed, "the Makefile no longer declares a single ASSETS list"
    referenced = set(re.findall(r"\$\((\w+_ASSET)\)", listed.group(1)))
    missing = set(declared) - referenced
    assert not missing, (
        f"{sorted(missing)} is staged but absent from ASSETS, so the __pycache__, "
        "bin/ and RECORD cleanups skip it — which AgentCore Runtime rejects outright"
    )


def test_the_package_recipe_strips_bytecode_from_every_asset() -> None:
    """The recipe half. Meaningful in CI, where nothing is staged to inspect."""
    recipe = MAKEFILE.read_text(encoding="utf-8")
    assert "$(ASSETS) -name __pycache__" in recipe, (
        "make package no longer strips __pycache__ from every asset — AgentCore "
        "Runtime refuses an artifact containing it (V10-5)"
    )
    assert "-name '*.pyc'" in recipe, "loose .pyc files are not stripped"


def test_every_declared_asset_is_gitignored_except_its_marker() -> None:
    """The other half of V10-5, and the more expensive one.

    `.gitignore` carries THREE rules per asset: un-ignore the directory (because
    `build/` hides it), ignore its contents, un-ignore `.gitkeep`. An asset that gets
    none is not merely untracked — it is untracked *and unignored*, so `git add -A`
    commits 131 MB of vendored `langchain` and ruff lints third-party source. This
    was caught by ruff reporting N818 on botocore, which is a strange way to learn
    about a packaging bug and exactly why it is worth a guard.
    """
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    for asset in ASSETS:
        relative = asset.relative_to(REPO).as_posix()
        for rule in (f"!{relative}/", f"{relative}/*", f"!{relative}/.gitkeep"):
            assert rule in gitignore, f"{relative} is missing the .gitignore rule {rule!r}"


def test_every_declared_asset_ships_a_tracked_marker() -> None:
    """`cdk synth` resolves `Code.from_asset` / `s3_assets.Asset` against these paths,
    so the directory must exist in a fresh clone — before anyone runs `make package`.
    Without the marker the hermetic gate fails on checkout."""
    for asset in ASSETS:
        assert (asset / ".gitkeep").is_file(), f"{asset.name} has no tracked .gitkeep"
