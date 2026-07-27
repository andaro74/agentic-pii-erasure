"""Integration preflight: both code-bearing stacks must match the working tree.

Same V7-2 defence as the conformance suite, extended to the saga stack — an
integration failure against a stale saga executor reads as "the saga is broken" when
the truth is "the fix was never deployed", and that wrong answer sends you back to
correct code. The check is loaded from the conformance conftest so there is exactly
one implementation to keep honest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "conformance_preflight", REPO / "tests" / "conformance" / "conftest.py"
)
assert _spec is not None
assert _spec.loader is not None
_preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_preflight)

#: The integration suite drives the saga, which drives the participants — both
#: stacks' bytes must be the working tree's.
CODE_BEARING_STACKS = ("participants", "saga")


@pytest.fixture(scope="session", autouse=True)
def deployed_code_matches_the_working_tree() -> None:
    for stack in CODE_BEARING_STACKS:
        _preflight.assert_stack_matches_working_tree(stack)
