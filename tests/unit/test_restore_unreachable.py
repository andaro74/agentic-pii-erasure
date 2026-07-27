"""Invariant 6: `restore` is unreachable from any phase-3 code path.

A compensating transaction that recreates a subject's data converts a failed erasure
into an active breach. Checked at two independent seams:

* **routing** — the declarative path maps for the phase-3 nodes contain no edge to
  `compensate`, and their routers cannot *return* a label that maps to it;
* **source** — the phase-3 modules reference no `restore`/`compensate` identifier and
  import nothing from the compensate module. AST identifiers, not substrings, so the
  docstrings that explain the rule cannot satisfy it.

Do not weaken either check. If a future change appears to need phase 3 to restore
something, the change is wrong, not the test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pii_erasure.saga.edges import PATH_MAPS, PHASE3_NODES, ROUTERS

SRC = Path(__file__).resolve().parents[2] / "src" / "pii_erasure"

PHASE3_MODULES = (
    SRC / "saga" / "nodes" / "hard_delete.py",
    SRC / "saga" / "nodes" / "verify.py",
    SRC / "saga" / "nodes" / "sweep.py",
)

_FORBIDDEN_FRAGMENTS = ("restore", "compensate")


def test_phase3_path_maps_never_route_to_compensate() -> None:
    for node in PHASE3_NODES:
        if node not in PATH_MAPS:
            continue  # a terminal direct edge (sweep → END) has no conditional row
        targets = set(PATH_MAPS[node].values())
        assert "compensate" not in targets, (
            f"{node} routes to compensate — phase 3 never compensates (invariant 6)"
        )


def test_every_conditional_route_out_of_phase3_stays_forward() -> None:
    """The routers' possible return labels must all exist in their path map — a label
    outside the map would crash at runtime, which is better than a wrong edge, but
    the point here is the composition: labels → targets → never compensate."""
    from tests.conftest import build_fixture_manifest

    manifest = build_fixture_manifest(saga_id="saga_p3", subject_ref="sub_p3")
    dumped = manifest.model_dump(mode="json", by_alias=True)
    all_receipts = {f"hard_delete:{p.system_id}": {} for p in manifest.participants}

    probes = [
        {"status": "aborted", "manifest": dumped},
        {"status": "running", "manifest": dumped, "receipts": {}},
        {"status": "running", "manifest": dumped, "receipts": all_receipts},
        {"status": "stuck", "manifest": dumped, "receipts": all_receipts},
    ]
    for node in PHASE3_NODES:
        if node not in ROUTERS:
            continue
        labels = set(PATH_MAPS[node])
        for probe in probes:
            label = ROUTERS[node](probe)
            assert label in labels


@pytest.mark.parametrize("path", PHASE3_MODULES, ids=lambda p: p.name)
def test_phase3_sources_reference_no_restore_identifier(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                assert not any(f in module.lower() for f in _FORBIDDEN_FRAGMENTS), (
                    f"{path.name} imports {module} (invariant 6)"
                )
        elif isinstance(node, ast.Name | ast.Attribute):
            name = (node.id if isinstance(node, ast.Name) else node.attr).lower()
            assert not any(f in name for f in _FORBIDDEN_FRAGMENTS), (
                f"{path.name} references identifier containing 'restore'/'compensate' (invariant 6)"
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            assert not any(f in node.name.lower() for f in _FORBIDDEN_FRAGMENTS)


def test_the_phase3_modules_exist() -> None:
    for path in PHASE3_MODULES:
        assert path.is_file(), f"{path} is missing — is this test aimed at the right tree?"
