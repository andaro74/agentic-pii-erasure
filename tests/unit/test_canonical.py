"""Canonicalisation stability — CLAUDE.md invariant 4, ADR-006's foundation.

An approval binds to `sha256(canonical(manifest))`. If two semantically identical plans
canonicalise to different bytes, a still-correct human approval silently stops matching
and the digest binding means nothing. So these tests are not "does the serialiser work";
they are "can the same plan ever produce two different digests", asked from as many
angles as we can think of.

Everything here is hermetic. No AWS, no network, no clock.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pii_erasure.contract.canonical import (
    SCHEMA_VERSION,
    VOLATILE_KEYS,
    CanonicalisationError,
    canonical,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canonical"

_PLAN: dict[str, Any] = {
    "sagaId": "saga_01JQ8",
    "subjectRef": "sub_a3f9",
    "participants": [
        {
            "systemId": "compliance-archive",
            "archetype": "WORM",
            "plannedOps": ["soft_delete", "hard_delete"],
            "artifacts": [
                {"kind": "object", "locator": "s3://archive/2026/", "count": 12},
                {"kind": "object", "locator": "s3://archive/2025/", "count": 3},
            ],
            "holds": [],
        }
    ],
    "graceWindowDays": 30,
}


def _shuffled(value: Any, rng: random.Random) -> Any:
    """Rebuild `value` with every object's keys inserted in a different order."""
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffled(child, rng) for key, child in items}
    if isinstance(value, list):
        return [_shuffled(child, rng) for child in value]
    return value


# ─── Rule 1 · key order is not information ────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(25))
def test_shuffled_key_order_produces_identical_bytes(seed: int) -> None:
    rng = random.Random(seed)
    assert canonical(_shuffled(_PLAN, rng)) == canonical(_PLAN)


def test_every_permutation_of_a_small_object_agrees() -> None:
    # Exhaustive rather than sampled, so this cannot pass by luck.
    keys = ["kind", "locator", "count", "classification"]
    values: dict[str, Any] = {
        "kind": "row",
        "locator": "public.orders",
        "count": 412,
        "classification": ["PII", "FINANCIAL"],
    }
    digests = {
        canonical({key: values[key] for key in order}) for order in itertools.permutations(keys)
    }
    assert len(digests) == 1


def test_non_ascii_keys_are_refused() -> None:
    with pytest.raises(CanonicalisationError) as raised:
        canonical({"café": 1})
    assert raised.value.path == "$.café"


# ─── Rule 2 · order is meaning, except where it is not ────────────────────────────────


def test_ordered_arrays_keep_their_order() -> None:
    forward = canonical({"plannedOps": ["soft_delete", "hard_delete"]})
    reverse = canonical({"plannedOps": ["hard_delete", "soft_delete"]})
    assert forward != reverse, "reordering a plan must change the digest (§8.3)"


def test_set_like_arrays_are_sorted() -> None:
    unsorted = [{"kind": "row", "locator": "b"}, {"kind": "row", "locator": "a"}]
    one = canonical({"artifacts": unsorted})
    other = canonical({"artifacts": list(reversed(unsorted))})
    assert one == other
    assert one.index(b'"a"') < one.index(b'"b"')


def test_scalar_set_like_arrays_are_sorted() -> None:
    assert canonical({"classification": ["PII", "FINANCIAL"]}) == canonical(
        {"classification": ["FINANCIAL", "PII"]}
    )


def test_holds_sort_by_hold_id() -> None:
    holds = [
        {"holdId": "LIT-2024-118", "authority": "Legal"},
        {"holdId": "LIT-2023-002", "authority": "Legal"},
    ]
    assert canonical({"holds": holds}) == canonical({"holds": list(reversed(holds))})


def test_set_like_elements_identical_on_their_sort_keys_are_still_stable() -> None:
    # Same kind and locator, different counts: the element's own bytes are the final
    # tiebreaker, so the ordering is total and cannot flap.
    items = [
        {"kind": "row", "locator": "t", "count": 2},
        {"kind": "row", "locator": "t", "count": 1},
    ]
    assert canonical({"artifacts": items}) == canonical({"artifacts": list(reversed(items))})


def test_a_set_like_array_of_scalars_where_objects_are_expected_is_refused() -> None:
    with pytest.raises(CanonicalisationError):
        canonical({"artifacts": ["not-an-object"]})


# ─── Rule 3 · numbers ─────────────────────────────────────────────────────────────────


def test_integers_are_normalised_not_reformatted() -> None:
    assert canonical({"count": 412}) == b'{"count":412}'
    assert canonical({"count": -0}) == b'{"count":0}'
    assert canonical({"count": 10**30}) == b'{"count":' + str(10**30).encode() + b"}"


def test_floats_are_refused_rather_than_rounded() -> None:
    with pytest.raises(CanonicalisationError) as raised:
        canonical({"count": 412.0})
    assert "float" in str(raised.value)
    assert raised.value.path == "$.count"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_are_refused(value: float) -> None:
    with pytest.raises(CanonicalisationError):
        canonical({"count": value})


def test_booleans_are_not_integers() -> None:
    assert canonical({"found": True}) == b'{"found":true}'
    assert canonical({"found": False}) == b'{"found":false}'
    assert canonical({"found": 1}) != canonical({"found": True})


# ─── Rule 4 · strings ─────────────────────────────────────────────────────────────────


def test_composed_and_decomposed_unicode_agree() -> None:
    # Written as escapes on purpose: these two render identically in every editor,
    # which is exactly why the hazard is invisible without normalisation.
    composed = "café"  # e-acute as one code point
    decomposed = "café"  # e + combining acute
    assert composed != decomposed
    assert canonical({"locator": composed}) == canonical({"locator": decomposed})


def test_escaping_is_minimal_and_stable() -> None:
    assert canonical({"locator": 'a"b\\c'}) == b'{"locator":"a\\"b\\\\c"}'
    assert canonical({"locator": "tab\there"}) == b'{"locator":"tab\\there"}'
    assert canonical({"locator": "bell\x01"}) == b'{"locator":"bell\\u0001"}'
    # Non-ASCII survives as UTF-8 rather than \u-escapes, per JCS.
    assert canonical({"locator": "\U0001f600"}) == '{"locator":"\U0001f600"}'.encode()


# ─── Rule 5 · volatile keys are rejected, not dropped ─────────────────────────────────


@pytest.mark.parametrize("key", sorted(VOLATILE_KEYS))
def test_every_volatile_key_is_refused(key: str) -> None:
    with pytest.raises(CanonicalisationError) as raised:
        canonical({key: "anything"})
    assert raised.value.path == f"$.{key}"


def test_volatile_keys_are_refused_at_depth_not_just_at_the_top() -> None:
    body = {"participants": [{"systemId": "profile-store", "evidence": {"traceId": "1-abc"}}]}
    with pytest.raises(CanonicalisationError) as raised:
        canonical(body)
    assert raised.value.path == "$.participants[0].evidence.traceId"


def test_the_rejection_does_not_echo_the_value() -> None:
    """Invariant 5 applies to exception messages. This path is injection-reachable."""
    with pytest.raises(CanonicalisationError) as raised:
        canonical({"observedAt": "2026-07-23T10:14:02Z"})
    assert "2026-07-23" not in str(raised.value)


def test_a_timestamped_body_cannot_be_digested_at_all() -> None:
    """Invariant 4's operative clause: no timestamps, run IDs, trace IDs or Runtime
    session IDs inside a digested body — including all four at once."""
    for key in ("discoveredAt", "runId", "traceId", "runtimeSessionId"):
        with pytest.raises(CanonicalisationError):
            canonical({"sagaId": "saga_1", key: "x"})


# ─── Rule 6 · nulls, and other shapes ─────────────────────────────────────────────────


def test_explicit_null_is_not_an_absent_key() -> None:
    assert canonical({"retentionUntil": None}) != canonical({})
    assert canonical({"retentionUntil": None}) == b'{"retentionUntil":null}'


def test_empty_containers_survive() -> None:
    assert canonical({"holds": [], "artifacts": []}) == b'{"artifacts":[],"holds":[]}'


def test_unsupported_types_are_refused() -> None:
    for value in ({1, 2}, (1, 2), object()):
        with pytest.raises(CanonicalisationError):
            canonical({"locator": value})  # type: ignore[dict-item]


def test_nesting_is_bounded() -> None:
    deep: Any = "leaf"
    for _ in range(200):
        deep = {"nested": deep}
    with pytest.raises(CanonicalisationError) as raised:
        canonical(deep)
    assert "nesting" in raised.value.reason


# ─── Cross-process determinism ────────────────────────────────────────────────────────


def test_digest_is_identical_under_different_hash_seeds() -> None:
    """`PYTHONHASHSEED` randomises dict iteration-adjacent behaviour and set ordering.

    A canonicaliser that leaked either would produce a stable digest all day on one
    machine and a different one in Lambda — the worst possible failure shape, because it
    only appears once an approval is already outstanding.
    """
    program = (
        "import json,sys;"
        "sys.path.insert(0, r'src');"
        "from pii_erasure.contract.canonical import canonical;"
        "sys.stdout.write(canonical(json.loads(sys.argv[1])).decode())"
    )
    payload = json.dumps(_PLAN)
    digests = set()
    for seed in ("0", "1", "424242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", program, payload],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        digests.add(result.stdout)
    assert len(digests) == 1
    assert digests.pop().encode() == canonical(_PLAN)


# ─── Golden fixtures — the schemaVersion tripwire ─────────────────────────────────────


def _fixture_files() -> list[Path]:
    files = sorted(FIXTURES.glob("*.json"))
    assert files, "the golden fixtures are missing — invariant 4 has no tripwire"
    return files


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_golden_fixture(path: Path) -> None:
    """Pins the exact bytes for a known input.

    This is what makes a change to canonicalisation a *breaking* change rather than a
    silent one: touch the rules and these go red, and the fix is a `SCHEMA_VERSION` bump
    plus a new fixture — never an edited expectation.
    """
    import hashlib

    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["schemaVersion"] == SCHEMA_VERSION, (
        f"{path.name} was written for canonicalisation schema {fixture['schemaVersion']}; "
        f"the module is at {SCHEMA_VERSION}. Add a fixture for the new version, do not "
        f"edit this one."
    )

    produced = canonical(fixture["input"])
    assert produced.decode("utf-8") == fixture["canonical"]
    assert hashlib.sha256(produced).hexdigest() == fixture["sha256"]
