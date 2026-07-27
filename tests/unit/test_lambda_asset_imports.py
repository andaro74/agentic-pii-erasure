"""Every participant handler must import with the distribution **not installed**.

This exists because of V7-1. `pii_erasure/__init__.py` resolved its own version through
`importlib.metadata` at import time. That works everywhere a developer looks — the venv
has an editable install, so the metadata is right there — and fails in the only place
that matters: a Lambda, whose asset is a *copied source tree*, not an install. Every
participant died at cold start with `Runtime.ImportModuleError`, and `make conformance`
failed on its first call with no assertion ever reached.

The shape is the recurring one in this repo: **the gate tested the repository, not the
artifact.** `make check` imports `pii_erasure` from the venv; `cdk synth` checks the asset
directory exists but never imports from it. Nothing hermetic had ever executed the bytes
that get uploaded.

So this test reconstructs the deployed condition rather than describing it: the package is
copied to a scratch directory the way `make package` copies it, `sys.path` is rebuilt to
contain that copy instead of the repo, and `importlib.metadata` is made to report the
distribution as absent — which, in a Lambda, it genuinely is. Third-party dependencies stay
importable from site-packages because `make package` vendors them into the same asset; it
is the *project's own* metadata that does not travel.

Rerun protection, not simulation-for-its-own-sake: the one thing this cannot see is a
missing *dependency*, since site-packages supplies those here and the asset must vendor
them. `make conformance` remains the gate for that, and ADR-017 is why.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pii_erasure.contract import PARTICIPANTS

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "pii_erasure"
DISTRIBUTION = "agentic-pii-erasure"


#: Modules a Lambda cold start actually imports. Handlers are the entry points; the CLI is
#: deliberately absent, because it is the one component that *is* always pip-installed.
#: The saga-plane entry points (M5) are probed the same way — their asset is a copied
#: source tree exactly like the participants', and V7-1 applies to it identically.
_SAGA_ENTRYPOINTS = ("saga/handler.py", "scheduler/handler.py")


def _handler_modules() -> list[str]:
    built = [
        spec
        for spec in PARTICIPANTS
        if (PACKAGE / "participants" / spec.system_id.replace("-", "_") / "handler.py").is_file()
    ]
    assert built, "no participant handlers found — the test is looking in the wrong place"
    modules = [
        f"pii_erasure.participants.{spec.system_id.replace('-', '_')}.handler" for spec in built
    ]
    modules.extend(
        "pii_erasure." + entry.removesuffix(".py").replace("/", ".")
        for entry in _SAGA_ENTRYPOINTS
        if (PACKAGE / entry).is_file()
    )
    return modules


_PROBE = textwrap.dedent(
    """
    import importlib, importlib.metadata as md, sys

    # Rebuild the path the way a Lambda has it: the asset first and the repo's own source
    # roots gone, so a stray `src/` cannot quietly satisfy the import we are trying to
    # break. Only those two entries are dropped — the venv lives *inside* the repo, and
    # filtering the whole prefix would take site-packages with it, turning this into a
    # test about missing dependencies instead of missing metadata.
    _excluded = {{{repo!r}, {src!r}, ""}}
    sys.path = [{asset!r}] + [p for p in sys.path if p not in _excluded]

    # An editable install resolves through a *meta path* finder, which runs ahead of
    # sys.path entirely and would hand back the repo copy however the path is ordered.
    sys.meta_path = [
        f for f in sys.meta_path if "editable" not in type(f).__name__.lower()
    ]

    # The deployed truth: the asset is a copied source tree, so the project's own
    # distribution metadata is not present. Dependency metadata still is, because
    # `make package` vendors the dependencies into the asset alongside their dist-info.
    _real_version, _real_distribution = md.version, md.distribution


    def _version(name, *args, **kwargs):
        if name == {distribution!r}:
            raise md.PackageNotFoundError(name)
        return _real_version(name, *args, **kwargs)


    def _distribution(name, *args, **kwargs):
        if name == {distribution!r}:
            raise md.PackageNotFoundError(name)
        return _real_distribution(name, *args, **kwargs)


    md.version, md.distribution = _version, _distribution

    for module in {modules!r}:
        loaded = importlib.import_module(module)
        origin = getattr(loaded, "__file__", "")
        assert origin.startswith({asset!r}), (
            module + " resolved to " + origin + " instead of the staged asset"
        )

    print("ok")
    """
)


@pytest.fixture(scope="module")
def staged_asset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`make package`'s copy step, reproduced without its network install."""
    asset = tmp_path_factory.mktemp("lambda-asset")
    shutil.copytree(
        PACKAGE, asset / "pii_erasure", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return asset


def test_handlers_import_without_the_distribution_installed(staged_asset: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(
                distribution=DISTRIBUTION,
                asset=str(staged_asset),
                repo=str(REPO),
                src=str(REPO / "src"),
                modules=_handler_modules(),
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "a participant handler cannot be imported from a staged Lambda asset, so every "
        "cold start in the deployed stack will fail with Runtime.ImportModuleError "
        "(V7-1):\n" + result.stderr
    )
    assert "ok" in result.stdout


#: `importlib.metadata` describes an *installed distribution*. Calling one of these while a
#: module body executes makes "was this pip-installed?" a precondition of importing the
#: library at all — false for every artifact this repo ships to Lambda.
_METADATA_CALLS = frozenset({"version", "distribution", "metadata", "files", "requires"})


def _module_level_metadata_calls(tree: ast.Module) -> list[str]:
    """Names from `importlib.metadata` that are *called* while the module body runs.

    Function bodies are exempt: deferring the lookup to call time is the fix, not the bug.
    Class bodies are not, because they execute on import just as the module body does.
    """
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib.metadata":
            for alias in node.names:
                imported[alias.asname or alias.name] = alias.name

    deferred = {
        child
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for child in ast.walk(node)
    }

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node in deferred:
            continue
        func = node.func
        if isinstance(func, ast.Name) and imported.get(func.id) in _METADATA_CALLS:
            found.append(imported[func.id])
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in _METADATA_CALLS
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "metadata"
        ):
            found.append(func.attr)
    return found


@pytest.mark.parametrize(
    "module",
    [path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts],
    ids=lambda path: str(path.relative_to(PACKAGE)).replace("\\", "/"),
)
def test_no_module_reads_distribution_metadata_at_import(module: Path) -> None:
    """The same rule as above, enforced structurally rather than by execution.

    Two mechanisms because the executable probe only covers the modules a handler happens
    to reach today; this one covers every module in the package, including the ones a
    later milestone will put on the cold-start path.
    """
    calls = _module_level_metadata_calls(ast.parse(module.read_text(encoding="utf-8")))
    assert not calls, (
        f"{module.relative_to(REPO)} calls importlib.metadata.{calls[0]}() at import time, "
        "so importing it requires the distribution to be pip-installed — which it is not "
        "inside a Lambda asset (V7-1). Defer the lookup into a function."
    )
