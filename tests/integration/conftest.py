"""Integration preflight, and the fixtures both integration modules drive AWS through.

**Preflight:** the same V7-2 defence as the conformance suite, extended to the saga
stack — an integration failure against a stale saga executor reads as "the saga is
broken" when the truth is "the fix was never deployed", and that wrong answer sends you
back to correct code. The check is loaded from the conformance conftest so there is
exactly one implementation to keep honest.

**Fixtures:** `rig` and `lambda_client` live here rather than in `test_saga.py` because
`test_chaos.py` needs the same two, session-scoped and shared. Importing a fixture
between test modules works but shadows the name in every signature that consumes it,
which ruff reads as a redefinition and a reader reads as two fixtures. One definition,
one conftest.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[2]

STAGE = os.environ.get("PII_ERASURE_STAGE", "dev")
EXECUTOR = f"asdp-{STAGE}-saga-executor"
RESUME = f"asdp-{STAGE}-saga-resume"

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


def load_conformance() -> Any:
    """The conformance rig's writers, placements and cleanup, loaded as a module.

    By path rather than by import because `tests/conformance/test_contract.py` is a test
    module: reusing its proven seed/teardown code beats a second implementation that
    would drift from the one conformance actually grades against.
    """
    spec = importlib.util.spec_from_file_location(
        "conformance_rig", REPO / "tests" / "conformance" / "test_contract.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def rig() -> Any:
    module = load_conformance()
    from evals.fixtures.generator import FixtureGenerator
    from pii_erasure.cli.main import _seed_clients, _stack_config

    config = _stack_config(os.environ.get("PII_ERASURE_TENANT", "meridian"))
    # allow_ses_sandbox=True: in a sandbox account the contact still gets seeded and
    # the missing suppression entry is recorded as a degraded capability — which is
    # exactly what the saga will then observe. Deterministic in both account states.
    generator = FixtureGenerator(clients=_seed_clients(), config=config, allow_ses_sandbox=True)
    return module, generator, config


@pytest.fixture(scope="session")
def lambda_client() -> Any:
    # One invocation may legitimately run for minutes (Aurora resume, Athena).
    # retries=0 is load-bearing: a client-side retry of `invoke` would be a
    # duplicate saga step delivered by our own test harness.
    client = boto3.client(
        "lambda",
        config=Config(read_timeout=910, connect_timeout=10, retries={"max_attempts": 0}),
    )
    try:
        client.get_function(FunctionName=EXECUTOR)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        pytest.skip("saga stack is not deployed yet — run `make deploy-dev` (docs/ROADMAP.md M5)")
    return client
