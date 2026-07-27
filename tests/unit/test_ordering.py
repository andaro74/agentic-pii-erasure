"""§5.2's ordering rules, red and green.

The fixture manifest from tests/conftest.py is the green case; each rule's red case
is a targeted mutation of the slots — because a validator that has never rejected
anything is a validator you know nothing about.
"""

from __future__ import annotations

import pytest

from pii_erasure.manifest import Manifest, OrderSlot
from pii_erasure.saga.ordering import OrderingError, execution_order, validate_order
from tests.conftest import build_fixture_manifest


def _manifest() -> Manifest:
    return build_fixture_manifest(saga_id="saga_o", subject_ref="sub_o")


def _rerank(manifest: Manifest, system_id: str, *, phase: int, rank: int) -> Manifest:
    participants = tuple(
        p.model_copy(update={"order": OrderSlot(phase=phase, rank=rank)})
        if p.system_id == system_id
        else p
        for p in manifest.participants
    )
    return manifest.model_copy(update={"participants": participants, "digest": None})


def test_the_fixture_manifest_validates() -> None:
    validate_order(_manifest())


def test_phase2_puts_identity_first_by_construction() -> None:
    order = [p.system_id for p in execution_order(_manifest(), phase=2)]
    assert order[0] == "cognito-identity"
    assert len(order) == 8


def test_phase3_runs_derived_first_and_shred_last() -> None:
    order = [p.system_id for p in execution_order(_manifest(), phase=3)]
    assert order[0] == "vector-index"
    assert order[1] == "analytics-lake"
    assert order[-1] == "compliance-archive"


def test_a_hard_delete_outside_phase3_is_rejected() -> None:
    mutated = _rerank(_manifest(), "profile-store", phase=2, rank=0)
    with pytest.raises(OrderingError, match="outside phase 3"):
        validate_order(mutated)


def test_authoritative_before_derived_is_rejected() -> None:
    # Put Cognito (the join key) ahead of the vector index it joins to.
    mutated = _rerank(_manifest(), "cognito-identity", phase=3, rank=0)
    mutated = _rerank(mutated, "vector-index", phase=3, rank=50)
    with pytest.raises(OrderingError, match="derived stores before authoritative"):
        validate_order(mutated)


def test_an_early_shred_is_rejected() -> None:
    mutated = _rerank(_manifest(), "compliance-archive", phase=3, rank=0)
    with pytest.raises(OrderingError, match="last phase-3 step"):
        validate_order(mutated)


def test_unknown_phases_have_no_execution_order() -> None:
    with pytest.raises(OrderingError):
        execution_order(_manifest(), phase=1)
