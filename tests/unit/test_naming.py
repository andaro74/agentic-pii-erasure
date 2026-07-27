"""AgentCore name constraints — the guard that would have caught V10-1.

M6's hermetic gate was green and the deploy still failed, because `cdk synth` validates
the *template*, not the *service*: CloudFormation only checked `PolicyName` against its
resource schema during change-set validation, minutes into a paid deploy. Nothing in
`make check` had any opinion about what the control plane accepts.

Two layers, and the first is the one that matters:

1. **The literals still match the service.** `IDENTIFIER_PATTERN` and
   `IDENTIFIER_MAX_LENGTH` are re-read from the installed `bedrock-agentcore-control`
   model and compared. A hand-copied constraint that has drifted from the API is the
   same defect one level up — a guard describing a rule nobody re-checked.
2. **The helper refuses what the service would refuse**, at synth, where it is free.

`tests/unit/test_participants_synth.py` applies the constraint to the actual synthesized
names; this file is about the constraint itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import botocore.session
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))

from stacks.naming import (
    IDENTIFIER_MAX_LENGTH,
    IDENTIFIER_PATTERN,
    NameConstraintError,
    agentcore_identifier,
)

#: Every shape the underscore convention covers. `AgentRuntimeName` is here before the
#: Runtime exists (M7) precisely because that is when the constraint is cheap to learn.
UNDERSCORE_SHAPES = ("PolicyName", "PolicyEngineName", "AgentRuntimeName")

#: The shapes that take the `asdp-{stage}-…` form the rest of the stack uses. Asserted
#: too, so "hyphens are fine here" stays a checked claim rather than a remembered one.
HYPHEN_SHAPES = ("GatewayName", "TargetName")


@pytest.fixture(scope="module")
def model() -> object:
    return botocore.session.get_session().get_service_model("bedrock-agentcore-control")


# ─── 1. the literals still match the service ──────────────────────────────────────────


@pytest.mark.parametrize("shape_name", UNDERSCORE_SHAPES)
def test_the_pattern_matches_the_installed_service_model(model: object, shape_name: str) -> None:
    """If AWS changes the shape, this says so — rather than the next deploy saying so."""
    metadata = model.shape_for(shape_name).metadata  # type: ignore[attr-defined]
    pattern = metadata.get("pattern")
    # Some shapes express the cap inside the pattern (`{0,47}` after a leading char),
    # others as a separate `max`. Both mean 48; compare the character class, and check
    # the length separately below.
    assert pattern is not None, f"{shape_name} declares no pattern"
    assert pattern.startswith(("[A-Za-z][A-Za-z0-9_]", "[a-zA-Z][a-zA-Z0-9_]")), (
        f"{shape_name} is {pattern!r}; stacks/naming.py assumes {IDENTIFIER_PATTERN.pattern}"
    )


@pytest.mark.parametrize("shape_name", UNDERSCORE_SHAPES)
def test_the_length_cap_matches_the_installed_service_model(model: object, shape_name: str) -> None:
    metadata = model.shape_for(shape_name).metadata  # type: ignore[attr-defined]
    declared = metadata.get("max")
    if declared is None:
        # Expressed in the pattern instead: `[a-zA-Z][a-zA-Z0-9_]{0,47}` — one leading
        # character plus 47 more.
        assert metadata["pattern"].endswith(f"{{0,{IDENTIFIER_MAX_LENGTH - 1}}}"), (
            f"{shape_name} caps length differently from {IDENTIFIER_MAX_LENGTH}"
        )
    else:
        assert declared == IDENTIFIER_MAX_LENGTH


@pytest.mark.parametrize("shape_name", HYPHEN_SHAPES)
def test_the_hyphenated_shapes_really_do_accept_hyphens(model: object, shape_name: str) -> None:
    """Why `asdp-dev-gateway` deployed at M4 while `asdp-dev-policy-engine` did not."""
    import re

    pattern = model.shape_for(shape_name).metadata["pattern"]  # type: ignore[attr-defined]
    assert re.fullmatch(pattern, "asdp-dev-gateway"), (
        f"{shape_name} would reject the hyphenated form this stack uses for it"
    )


# ─── 2. the helper refuses what the service would refuse ──────────────────────────────


def test_a_hyphenated_source_is_translated() -> None:
    assert (
        agentcore_identifier("asdp", "dev", "05-discovery-never-mutates")
        == "asdp_dev_05_discovery_never_mutates"
    )


def test_a_leading_digit_is_refused_rather_than_repaired() -> None:
    with pytest.raises(NameConstraintError, match="leading letter"):
        agentcore_identifier("05-discovery-never-mutates")


def test_an_over_long_name_is_refused_rather_than_truncated() -> None:
    """Truncation is the tempting fix and the wrong one: two policies trimmed to the
    same 48 characters is one policy deployed and one silently absent."""
    with pytest.raises(NameConstraintError, match="characters"):
        agentcore_identifier("asdp", "dev", "x" * IDENTIFIER_MAX_LENGTH)


def test_the_boundary_length_is_allowed() -> None:
    name = agentcore_identifier("a" * IDENTIFIER_MAX_LENGTH)
    assert len(name) == IDENTIFIER_MAX_LENGTH
    assert IDENTIFIER_PATTERN.match(name)


@pytest.mark.parametrize("illegal", ["asdp.dev", "asdp/dev", "asdp-dev"])
def test_every_translated_character_produces_a_legal_name(illegal: str) -> None:
    assert IDENTIFIER_PATTERN.match(agentcore_identifier(illegal))


def test_an_untranslatable_character_still_fails() -> None:
    """The translation table is a whitelist of known separators, not a sanitiser. A
    space has no defensible mapping, so it stops the build instead of guessing one."""
    with pytest.raises(NameConstraintError):
        agentcore_identifier("asdp dev")
