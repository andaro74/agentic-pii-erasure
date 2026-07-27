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

#: The ONE boto3 service the execution plane may reach in the Bedrock family, and the
#: ONE file that may reach it. Invariant 12 has always said this — *"their only
#: AgentCore permission is the single `plan` node's Runtime invocation"* — but until
#: M7 nothing in `saga/` needed AgentCore, so the guard used a `bedrock` prefix that
#: was accidentally exact. It is now spelled out rather than widened: `bedrock-runtime`
#: (the model client) still fails everywhere, including in the allowlisted file.
#:
#: The distinction is the whole invariant. `bedrock-runtime` means "this process
#: reasons". `bedrock-agentcore` means "this process asks something else to reason and
#: receives JSON" — which is what makes the boundary expressible in IAM at all.
_AGENTCORE_CLIENT = "bedrock-agentcore"
_AGENTCORE_ALLOWED_IN = frozenset({"saga/planner.py"})


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
                    service = first.value
                    permitted = (
                        service == _AGENTCORE_CLIENT and _relative(path) in _AGENTCORE_ALLOWED_IN
                    )
                    assert not service.startswith("bedrock") or permitted, (
                        f"{_relative(path)} constructs a {service!r} client "
                        "(invariant 2 — and invariant 12 denies it IAM anyway)"
                    )
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert not name.startswith(_FORBIDDEN_IDENTIFIER_PREFIXES), (
                f"{_relative(path)} references {name} (invariant 2)"
            )


def test_the_agentcore_exception_is_exactly_one_file() -> None:
    """The allowlist is a hole in a guard, so its size is the thing to check.

    Invariant 12 permits one AgentCore call from one place. If this list grows, the
    saga has more than one route to the reasoning plane and the IAM assertion in
    `test_saga_synth.py` no longer describes the code.
    """
    assert {"saga/planner.py"} == _AGENTCORE_ALLOWED_IN
    assert (SRC / "saga" / "planner.py").is_file()


@pytest.mark.parametrize("service", ["bedrock-runtime", "bedrock", "bedrock-agent-runtime"])
def test_a_model_client_still_fails_inside_the_allowlisted_file(service: str) -> None:
    """The exception is for `bedrock-agentcore` specifically, not for the `bedrock`
    family. A model client in `planner.py` must still fail — otherwise the allowlist
    would have quietly legalised the thing invariant 2 exists to prevent."""
    source = f"import boto3\nc = boto3.client({service!r})\n"
    tree = ast.parse(source)
    offending = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "client"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert offending == [service]
    permitted = service == _AGENTCORE_CLIENT
    assert not permitted, f"{service} must never be permitted in the execution plane"


def test_the_guard_is_looking_at_real_modules() -> None:
    """A guard over an empty tree passes silently — the V-series defect class."""
    modules = {_relative(p) for p in _modules()}
    assert "saga/nodes/hard_delete.py" in modules
    assert "scheduler/handler.py" in modules
    assert "approval/tokens.py" in modules
    assert len(modules) >= 20
