"""The framework boundary is an explicit allowlist — CLAUDE.md invariant 0.

The point is not purity. Interrupts, middleware and resume genuinely need the framework,
and pretending otherwise would produce a worse system. The point is that `contract/`,
`manifest/`, `participants/`, `ledger/` and the policy *engine* stay framework-free,
because that is what made two framework migrations (ADR-009 → 011 → 013) touch almost
nothing. Widening the list below is an architectural decision, not a convenience import.

`boto3` is not boundaried the same way — this is an AWS-only platform and pretending
otherwise would be theatre. But `contract/` and `manifest/models.py` stay `boto3`-free,
because they are the two files a reader should be able to lift wholesale.

The allowlist is written out verbatim, matching CLAUDE.md word for word. A test that
computed it from the tree would pass no matter what the tree contained.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "pii_erasure"

#: Verbatim from CLAUDE.md invariant 0. Paths are relative to `src/pii_erasure/`;
#: a trailing slash means the whole package.
FRAMEWORK_ALLOWLIST: tuple[str, ...] = (
    "discovery/",
    "runtime/",
    "saga/",
    "policy/middleware.py",
    "approval/gate.py",
    "scheduler/handler.py",
)

FRAMEWORK_ROOTS = ("langgraph", "langchain", "langchain_aws", "langchain_core")

#: Packages that must not import the cloud SDK either. Narrower than the framework rule,
#: and deliberately so.
CLOUD_FREE = ("contract/", "manifest/models.py")


def _modules() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _allowed(relative: str, allowlist: tuple[str, ...]) -> bool:
    return any(
        relative.startswith(entry) if entry.endswith("/") else relative == entry
        for entry in allowlist
    )


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_framework_imports_stay_inside_the_allowlist(path: Path) -> None:
    relative = _relative(path)
    offending = _imported_roots(path) & set(FRAMEWORK_ROOTS)
    if offending and not _allowed(relative, FRAMEWORK_ALLOWLIST):
        pytest.fail(
            f"{relative} imports {sorted(offending)}, which invariant 0 permits only in "
            f"{list(FRAMEWORK_ALLOWLIST)}. Widening that list is an architectural "
            f"decision — write the ADR, do not add the import."
        )


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_the_liftable_packages_are_cloud_free(path: Path) -> None:
    relative = _relative(path)
    if not _allowed(relative, CLOUD_FREE):
        return
    offending = _imported_roots(path) & {"boto3", "botocore"}
    assert not offending, (
        f"{relative} imports {sorted(offending)}. contract/ and manifest/models.py are "
        f"the two files a reader should be able to lift wholesale."
    )


def test_the_contract_package_is_actually_covered() -> None:
    """A boundary test that inspected an empty tree would pass silently — the defect
    class docs/VALIDATION.md exists for. Assert there is something to protect."""
    covered = [path for path in _modules() if _relative(path).startswith("contract/")]
    assert len(covered) >= 6, (
        "contract/ is missing modules — is this test looking at the right tree?"
    )


def test_the_allowlist_matches_claude_md_verbatim() -> None:
    """The list above is the enforcement; CLAUDE.md is what a human reads. If they
    diverge, the invariant means two different things depending on where you look."""
    claude_md = (SRC.parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    for entry in FRAMEWORK_ALLOWLIST:
        assert f"`{entry}`" in claude_md, f"{entry} is enforced here but absent from CLAUDE.md"
