"""Shared fixtures. The hand-written M5 saga manifest lives here because both the
hermetic graph tests and the deployed integration suite replay the SAME plan —
ADR-001's claim is precisely that one manifest serves both."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pii_erasure.contract import Archetype, Artifact, Hold
from pii_erasure.manifest import (
    Manifest,
    ManifestParticipant,
    OrderSlot,
    Provenance,
    with_digest,
)

#: Phase-3 ranks (the OrderSlot positions the irreversible action — §7.1):
#: derived stores first while the join key lives, authoritative stores late,
#: crypto-shred dead last. Phase-2 ordering is structural (identity revokes first).
_PHASE3_RANKS: dict[str, int] = {
    "vector-index": 0,
    "analytics-lake": 1,
    "upload-bucket": 10,
    "notify-suppression": 11,
    "profile-store": 12,
    "billing-ledger": 20,
    "cognito-identity": 21,
    "compliance-archive": 99,
}

_ARCHETYPES: dict[str, Archetype] = {
    "cognito-identity": Archetype.AUTHORITATIVE_IDENTITY,
    "profile-store": Archetype.OPERATIONAL_NOSQL,
    "billing-ledger": Archetype.RELATIONAL,
    "upload-bucket": Archetype.DELETABLE_BLOB,
    "compliance-archive": Archetype.WORM,
    "vector-index": Archetype.DERIVED_INDEX,
    "analytics-lake": Archetype.COLUMNAR_ANALYTICS,
    "notify-suppression": Archetype.RESIDUAL_BY_DESIGN,
}


def build_fixture_manifest(
    *,
    saga_id: str,
    subject_ref: str,
    request_id: str = "dsr_fixture_0001",
    grace_window_days: int = 0,
    legal_holds: tuple[Hold, ...] = (),
    system_ids: tuple[str, ...] | None = None,
    extra_participants: tuple[ManifestParticipant, ...] = (),
) -> Manifest:
    """The hand-written plan an M5 saga replays. Digested, unsigned — the saga's own
    plan node signs it against the real CMK, exercising the M3 path in situ.

    `extra_participants` exists for failure-injection scenarios: a participant the
    registry does not know (a "ghost") is a real, honest way to make one phase fail
    against a deployed stack without mocking anything.
    """
    included = system_ids if system_ids is not None else tuple(_PHASE3_RANKS)
    participants = []
    for system_id in included:
        archetype = _ARCHETYPES[system_id]
        participants.append(
            ManifestParticipant(
                system_id=system_id,
                archetype=archetype,
                artifacts=(Artifact(kind="subject-data", locator=f"{system_id}:{subject_ref}"),),
                planned_ops=("soft_delete", "hard_delete"),
                order=OrderSlot(phase=3, rank=_PHASE3_RANKS[system_id]),
                delete_method=("CRYPTO_SHRED" if archetype is Archetype.WORM else "PURGE"),
                dek_registry_ref=(f"kr#{subject_ref}" if archetype is Archetype.WORM else None),
            )
        )
    manifest = Manifest(
        manifest_id=f"man_{saga_id}",
        saga_id=saga_id,
        subject_ref=subject_ref,
        request_id=request_id,
        provenance=Provenance(
            discovered_at="2026-07-26T00:00:00Z",
            agent_version="fixture-manifest@m5",
        ),
        participants=tuple(participants) + extra_participants,
        legal_holds=legal_holds,
        grace_window_days=grace_window_days,
    )
    return with_digest(manifest)


@pytest.fixture
def fixture_manifest() -> Callable[..., Manifest]:
    return build_fixture_manifest


@pytest.fixture
def fixture_manifest_dict() -> Callable[..., dict[str, Any]]:
    def _build(**kwargs: Any) -> dict[str, Any]:
        return build_fixture_manifest(**kwargs).model_dump(mode="json", by_alias=True)

    return _build
