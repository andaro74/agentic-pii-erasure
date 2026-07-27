"""Execution ordering (ARCHITECTURE §5.2) — each rule prevents an observed failure.

A participant carries ONE `order` slot, and §7.1's worked example shows what it means:
`compliance-archive` plans both `soft_delete` and `hard_delete` yet sits at
``{"phase": 3, "rank": 99}`` — the slot positions the participant's *irreversible*
action. Phase 2 therefore has no ranks of its own; its one ordering rule is
structural and is enforced by construction here:

1. **Phase 2: revoke first.** The identity participant soft-deletes before everything
   else, or in-flight sessions keep writing into systems already soft-deleted and
   verification fails for reasons unrelated to deletion.
2. **Phase 3: derived before authoritative.** The authoritative record is the join
   key. Purge Cognito or the Aurora parent row first and a failed `vector-index`
   deletion leaves embeddings present and permanently unaddressable — S3 Vectors has
   no delete-by-query, so a store you cannot enumerate is a store you cannot erase.
3. **Crypto-shred last.** DEK destruction is the only genuinely unrecoverable step;
   it runs after every other participant has reported success.

Rules 2 and 3 are properties of the digested ranks and are *validated*; a manifest
that violates them fails at plan time, before an approver ever sees it.

Pure functions over manifest data. No boto3, no framework.
"""

from __future__ import annotations

from pii_erasure.contract import Archetype
from pii_erasure.manifest import Manifest, ManifestParticipant

#: Stores whose contents are derived from an authoritative record elsewhere. They go
#: first in phase 3, while the join key still exists.
DERIVED_ARCHETYPES: frozenset[Archetype] = frozenset(
    {Archetype.DERIVED_INDEX, Archetype.COLUMNAR_ANALYTICS}
)

#: Stores that *are* the join key. They go last among the purges — but still before
#: the shred.
AUTHORITATIVE_ARCHETYPES: frozenset[Archetype] = frozenset(
    {Archetype.AUTHORITATIVE_IDENTITY, Archetype.RELATIONAL}
)


class OrderingError(ValueError):
    """A manifest whose order slots would reproduce a known failure."""


def execution_order(manifest: Manifest, *, phase: int) -> tuple[ManifestParticipant, ...]:
    """Participants to visit in one phase, in execution order.

    Phase 2: everyone planning a `soft_delete`, identity archetype first (rule 1 by
    construction), then rank and `system_id` for a total, deterministic order.

    Phase 3: everyone planning a `hard_delete`, by rank then `system_id`. Replay must
    visit participants in exactly the order the approver saw.
    """
    if phase == 2:
        planned = [p for p in manifest.participants if "soft_delete" in p.planned_ops]
        return tuple(
            sorted(
                planned,
                key=lambda p: (
                    0 if p.archetype is Archetype.AUTHORITATIVE_IDENTITY else 1,
                    p.order.rank,
                    p.system_id,
                ),
            )
        )
    if phase == 3:
        planned = [p for p in manifest.participants if "hard_delete" in p.planned_ops]
        return tuple(sorted(planned, key=lambda p: (p.order.rank, p.system_id)))
    raise OrderingError(f"no execution order is defined for phase {phase}")


def validate_order(manifest: Manifest) -> None:
    """Reject a manifest whose ordering would lose the join key or shred early.

    Called at plan time (nodes/plan.py) so a defective hand-written or agent-produced
    plan never reaches the approver, let alone phase 3.
    """
    _validate_hard_deletes_sit_in_phase3(manifest)
    _validate_derived_before_authoritative(manifest)
    _validate_shred_last(manifest)


def _hard_deleting(manifest: Manifest) -> list[ManifestParticipant]:
    return [p for p in manifest.participants if "hard_delete" in p.planned_ops]


def _validate_hard_deletes_sit_in_phase3(manifest: Manifest) -> None:
    misplaced = [p.system_id for p in _hard_deleting(manifest) if p.order.phase != 3]
    if misplaced:
        raise OrderingError(
            f"{misplaced} plan a hard_delete but sit outside phase 3 — the slot "
            "positions the irreversible action (§7.1), and an irreversible action "
            "before the approval gate is exactly what the gate exists to prevent"
        )


def _validate_derived_before_authoritative(manifest: Manifest) -> None:
    planned = _hard_deleting(manifest)
    derived = [p for p in planned if p.archetype in DERIVED_ARCHETYPES]
    authoritative = [p for p in planned if p.archetype in AUTHORITATIVE_ARCHETYPES]
    if not derived or not authoritative:
        return
    latest_derived = max(p.order.rank for p in derived)
    earliest_authoritative = min(p.order.rank for p in authoritative)
    if latest_derived >= earliest_authoritative:
        raise OrderingError(
            "phase 3 must purge derived stores before authoritative ones — the "
            "authoritative record is the join key, and losing it leaves derived "
            "artifacts present and unaddressable (§5.2)"
        )


def _validate_shred_last(manifest: Manifest) -> None:
    planned = _hard_deleting(manifest)
    shreds = [p for p in planned if p.delete_method == "CRYPTO_SHRED"]
    others = [p for p in planned if p.delete_method != "CRYPTO_SHRED"]
    if not shreds or not others:
        return
    earliest_shred = min(p.order.rank for p in shreds)
    latest_other = max(p.order.rank for p in others)
    if earliest_shred <= latest_other:
        raise OrderingError(
            "crypto-shred must be the last phase-3 step — DEK destruction is the only "
            "genuinely unrecoverable operation and runs after everything else succeeded"
        )
