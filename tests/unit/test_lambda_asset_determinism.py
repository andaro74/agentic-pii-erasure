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

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"

ASSETS = (REPO / "infra" / "build" / "participants", REPO / "infra" / "build" / "saga")

#: Extensions that are host-specific by construction. `.pyc` is here for the same
#: reason as `bin/`: bytecode embeds a source path and a timestamp.
_HOST_SPECIFIC_SUFFIXES = (".exe", ".pyc", ".pyo")


def _staged(asset: Path) -> bool:
    """True when `make package` has actually run — the dir ships empty but tracked."""
    return asset.is_dir() and any(p.name != ".gitkeep" for p in asset.iterdir())


def test_the_package_recipe_strips_console_scripts() -> None:
    """Always meaningful, including in CI where nothing is staged."""
    recipe = MAKEFILE.read_text(encoding="utf-8")
    assert "$(LAMBDA_ASSET)/bin $(SAGA_ASSET)/bin" in recipe, (
        "make package no longer strips bin/ — the asset hash becomes a function of "
        "the build machine and the staleness preflight starts crying wolf (V9-1)"
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
