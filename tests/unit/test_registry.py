"""The participant registry — the thing conformance parameterises over.

If this drifts from ARCHITECTURE §4.2, a participant either goes untested or is tested
against the wrong expectations, and nobody finds out until a deployed gate.
"""

from __future__ import annotations

import pytest

from pii_erasure.contract import PARTICIPANTS, Archetype
from pii_erasure.contract.registry import get, system_ids


def test_there_are_eight_participants() -> None:
    assert len(PARTICIPANTS) == 8


def test_system_ids_are_unique() -> None:
    assert len(set(system_ids())) == len(PARTICIPANTS)


def test_every_archetype_is_taught_exactly_once() -> None:
    """Eight participants, eight archetypes: the registry is the pedagogical spine, so a
    duplicated archetype means a lesson was silently dropped."""
    archetypes = [spec.archetype for spec in PARTICIPANTS]
    assert sorted(archetypes, key=lambda a: a.value) == sorted(Archetype, key=lambda a: a.value)


def test_the_two_participants_that_cannot_fully_delete_are_named() -> None:
    """Invariant 7 is only meaningful if the honest exceptions are explicit. Iceberg rows
    survive until snapshot expiry; the SES suppression hash is legally retained."""
    residual = {spec.system_id for spec in PARTICIPANTS if spec.expects_residual}
    assert residual == {"analytics-lake", "notify-suppression"}


def test_every_participant_carries_its_lesson() -> None:
    assert all(spec.lesson for spec in PARTICIPANTS)


def test_lookup_by_system_id() -> None:
    spec = get("compliance-archive")
    assert spec.archetype is Archetype.WORM
    assert "Object Lock" in spec.aws_service


def test_an_unknown_system_id_fails_loudly() -> None:
    """A manifest naming a system the platform cannot reach must not be shrugged off —
    continuing would report an erasure that never happened."""
    with pytest.raises(KeyError, match="unknown systemId"):
        get("shadow-warehouse")


def test_specs_are_immutable() -> None:
    # dataclasses.FrozenInstanceError subclasses AttributeError.
    with pytest.raises(AttributeError):
        PARTICIPANTS[0].system_id = "mutated"  # type: ignore[misc]
