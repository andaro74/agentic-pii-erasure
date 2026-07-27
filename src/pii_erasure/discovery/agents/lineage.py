"""Lineage Tracer — follow derived-store dependencies.

A derived store is one whose contents came from somewhere else: `vector-index` holds
embeddings computed from `profile-store` rows; `analytics-lake` holds rows copied from
the operational systems. Two consequences, and the second is the one that bites.

**Ordering.** Derived stores must be purged before their authoritative source, or the
pipeline that built them rebuilds them from data that has not been deleted yet. The
saga's `ordering.py` enforces this at execution; lineage is where the relationship is
*discovered* so the manifest can carry it.

**Orphans.** A derived store outlives its source. Delete the profile row today and the
embedding is still there tomorrow, pointing at a subject who no longer exists anywhere
else — which is why `evals/fixtures/generator.py` deliberately seeds a subject whose
data lives *only* in a derived index. An agent that reasons "the source is empty, so the
derived store must be too" produces a false negative on exactly that fixture. Nothing
here infers absence from a source's absence; every relationship is additive.

The relationships come from two places: what participants declare in their `discover`
responses (`derivedFrom`), and the registry's archetypes. Declared beats inferred — a
participant knows its own lineage better than a static table does — but the archetype
table means a participant that declares nothing still gets its known relationships.
"""

from __future__ import annotations

from collections.abc import Sequence

from pii_erasure.contract.archetypes import Archetype
from pii_erasure.contract.registry import PARTICIPANTS
from pii_erasure.discovery.agents.prospector import ProbeResult

#: Archetypes whose contents are computed from another system's. Used only to seed
#: relationships when a participant declares none — never to override a declaration.
_DERIVED_ARCHETYPES = frozenset({Archetype.DERIVED_INDEX, Archetype.COLUMNAR_ANALYTICS})


def _archetype_of(system_id: str) -> Archetype | None:
    for spec in PARTICIPANTS:
        if spec.system_id == system_id:
            return spec.archetype
    return None


def derived_relationships(results: Sequence[ProbeResult]) -> tuple[tuple[str, str], ...]:
    """Return `(derived_system, source_system)` pairs found in this sweep.

    Only systems that actually reported data appear: a relationship pointing at a
    system holding nothing is noise in the manifest, and the approver's attention is
    a real budget (§8.1). Sorted for a byte-stable manifest — canonicalisation is
    downstream of this and invariant 4 does not forgive unstable ordering.
    """
    present = {r.system_id for r in results if r.found and not r.errored}
    pairs: set[tuple[str, str]] = set()

    for result in results:
        if result.errored or not result.found:
            continue
        # 1. Declared lineage. A participant naming its own source is authoritative.
        for artifact in result.artifacts:
            source = artifact.get("derivedFrom")
            if isinstance(source, str) and source and source != result.system_id:
                pairs.add((result.system_id, source))
        # 2. Archetype-seeded lineage, only where nothing was declared for this system.
        if any(pair[0] == result.system_id for pair in pairs):
            continue
        if _archetype_of(result.system_id) in _DERIVED_ARCHETYPES:
            for other in sorted(present):
                if other != result.system_id and _archetype_of(other) not in _DERIVED_ARCHETYPES:
                    pairs.add((result.system_id, other))
    return tuple(sorted(pairs))
