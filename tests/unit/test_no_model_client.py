"""Invariant 2, enforced on the source: the execution plane never holds a model client.

Three claims, each checked by AST rather than substring so a docstring mentioning the
words cannot satisfy or trip the guard:

1. nothing under `saga/`, `scheduler/`, or `approval/` imports `langchain*` — the
   model-client packages. (`langgraph` is the *orchestration* framework and is
   allowed where invariant 0 allows it; the model never arrives through it.)
2. nothing in those packages calls `boto3.client("bedrock…")` in any variant.
3. no identifier in those packages references a chat-model class (`ChatBedrock*`).

The IAM half of the same invariant (no `bedrock:*` on the roles) lives in
`test_saga_synth.py`; this is the belt to that suspender.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "pii_erasure"

EXECUTION_PLANE = ("saga", "scheduler", "approval")

_FORBIDDEN_IMPORT_ROOTS = {"langchain", "langchain_aws", "langchain_core", "langchain_community"}
_FORBIDDEN_IDENTIFIER_PREFIXES = ("ChatBedrock",)


def _modules() -> list[Path]:
    paths: list[Path] = []
    for package in EXECUTION_PLANE:
        paths.extend((SRC / package).rglob("*.py"))
    return sorted(p for p in paths if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


@pytest.mark.parametrize("path", _modules(), ids=_relative)
def test_no_model_client_in_the_execution_plane(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
            assert not (roots & _FORBIDDEN_IMPORT_ROOTS), (
                f"{_relative(path)} imports a model-client package — nodes replay "
                "manifests, they never reason (invariant 2)"
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            assert root not in _FORBIDDEN_IMPORT_ROOTS, (
                f"{_relative(path)} imports from {node.module} (invariant 2)"
            )
        elif isinstance(node, ast.Call):
            # boto3.client("bedrock-runtime") in any spelling of the target.
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "client" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    assert not first.value.startswith("bedrock"), (
                        f"{_relative(path)} constructs a {first.value!r} client "
                        "(invariant 2 — and invariant 12 denies it IAM anyway)"
                    )
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert not name.startswith(_FORBIDDEN_IDENTIFIER_PREFIXES), (
                f"{_relative(path)} references {name} (invariant 2)"
            )


def test_the_guard_is_looking_at_real_modules() -> None:
    """A guard over an empty tree passes silently — the V-series defect class."""
    modules = {_relative(p) for p in _modules()}
    assert "saga/nodes/hard_delete.py" in modules
    assert "scheduler/handler.py" in modules
    assert "approval/tokens.py" in modules
    assert len(modules) >= 20
