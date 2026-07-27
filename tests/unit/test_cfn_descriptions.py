"""Every `Description` in every stack is a string the narrowest service accepts (V10-2).

This repo writes em dashes everywhere, because its prose is part of the point. Four of
them reached CloudFormation resource descriptions, and IAM refuses them: `CreateRole`
constrains `Description` to `[\\u0009\\u000A\\u000D\\u0020-\\u007E\\u00A1-\\u00FF]*` —
tab, newline, carriage return, printable ASCII, and the Latin-1 supplement. An em dash
(U+2014) and a horizontal ellipsis (U+2026) are in none of those ranges.

**The constraint is per-service, which is what made this hard to see.** Lambda's
`Description` accepts anything; the resume Lambda deployed with `Command(resume=…)` in
its description at M5 and worked. So the same character is fine in one resource and a
400 in another, and "it deployed last time" proves nothing about the next resource that
gets one. CDK's own `CloudFormation-Validate` emits an `F3031` **warning** at synth —
which is easy to read as cosmetic, and was read that way here, one commit before it
failed a deploy.

So the rule this file enforces is deliberately stricter than any single service demands:
**every `Description` anywhere in any template satisfies IAM's pattern**, because IAM's
is the narrowest, and because a description is one refactor away from moving between
resource types. The pattern is read from the installed `iam` service model rather than
copied, for the same reason as `test_naming.py` — a hand-copied constraint is a claim
about an API nobody re-checked.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import botocore.session
import pytest
from aws_cdk import App

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.foundation import FoundationStack
from stacks.gateway import GatewayStack
from stacks.participants import ParticipantsStack
from stacks.saga import SagaStack


@pytest.fixture(scope="module")
def description_pattern() -> re.Pattern[str]:
    """IAM's `roleDescriptionType` — the narrowest `Description` constraint in the stack."""
    shape = (
        botocore.session.get_session()
        .get_service_model("iam")
        .operation_model("CreateRole")
        .input_shape.members["Description"]
    )
    return re.compile(f"^(?:{shape.metadata['pattern']})$")


@pytest.fixture(scope="module")
def templates() -> dict[str, dict[str, Any]]:
    """Every stack the app deploys — a per-stack fixture would miss the one not covered."""
    app = App()
    foundation = FoundationStack(app, "asdp-t-foundation", stage="t", object_lock_days=1)
    participants = ParticipantsStack(
        app,
        "asdp-t-participants",
        stage="t",
        object_lock_days=1,
        dek_registry=foundation.dek_registry,
        idempotency=foundation.idempotency,
    )
    saga = SagaStack(
        app,
        "asdp-t-saga",
        stage="t",
        checkpoints=foundation.checkpoints,
        checkpoint_offload=foundation.checkpoint_offload,
        ledger=foundation.ledger,
        tombstones=foundation.tombstones,
        idempotency=foundation.idempotency,
        signing_key=foundation.signing_key,
        participants=participants.functions,
    )
    gateway = GatewayStack(app, "asdp-t-gateway", stage="t", participants=participants.functions)
    synthesized = app.synth()
    return {
        stack.stack_name: json.loads(
            json.dumps(synthesized.get_stack_by_name(stack.stack_name).template)
        )
        for stack in (foundation, participants, saga, gateway)
    }


def _descriptions(node: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every `Description` string anywhere in a template, with where it lives."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Description" and isinstance(value, str):
                yield f"{path}/{key}", value
            yield from _descriptions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _descriptions(value, f"{path}[{index}]")


def test_the_app_actually_has_descriptions_to_check(
    templates: dict[str, dict[str, Any]],
) -> None:
    """A scan that finds nothing passes vacuously — which is how a guard stops guarding."""
    found = [item for name, template in templates.items() for item in _descriptions(template, name)]
    assert len(found) >= 20, f"only {len(found)} descriptions found; the scan is not reaching them"


def test_every_description_survives_the_narrowest_service_constraint(
    templates: dict[str, dict[str, Any]], description_pattern: re.Pattern[str]
) -> None:
    """IAM returns a 400 for an em dash. CDK warns (`F3031`); nothing failed the build."""
    offences: list[str] = []
    for name, template in templates.items():
        for where, text in _descriptions(template, name):
            if not description_pattern.match(text):
                illegal = sorted({c for c in text if not description_pattern.match(c)})
                offences.append(f"{where}: {text!r} contains {illegal!r}")
    assert not offences, "descriptions CloudFormation would reject:\n  " + "\n  ".join(offences)


def test_the_pattern_really_does_reject_the_characters_this_repo_writes(
    description_pattern: re.Pattern[str],
) -> None:
    """Prove the guard can go red. Without this, a pattern that matched everything would
    make the test above pass forever while enforcing nothing."""
    # Escaped rather than literal: the em dash, ellipsis, en dash and curly quote this
    # repo's prose uses. Writing them literally here would trip the linter that stops
    # them reaching source in the first place.
    for character in ("\u2014", "\u2026", "\u2013", "\u201c"):
        assert not description_pattern.match(f"ASDP thing {character} note"), (
            f"{character!r} was accepted; the constraint is not the one IAM applies"
        )
    assert description_pattern.match("ASDP thing - note")
