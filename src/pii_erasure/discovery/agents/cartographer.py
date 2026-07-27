"""CMDB Cartographer — enumerate the candidate systems.

The docs give this agent "Resource Explorer, tags, AWS Config" as its surface, and that
is the right long-term shape: in a real estate nobody knows what holds subject data, so
something has to go and look. In *this* platform the registry is authoritative — the
eight participants are the eight participants, declared in `contract/registry.py` — so
enumeration is not the open problem. Ordering is.

So the cartographer does the honest version of its job: it returns **every** registered
system, ordered by tenant priors. The function signature is the guarantee — there is no
parameter that can shorten the result, and `ordered_candidates` asserts the permutation
property on the way out.

This is invariant-13-adjacent and worth being explicit about: a prior may say "this
tenant's `vector-index` always mirrors `profile-store`, look there early". It may never
say "this tenant has no `billing-ledger`", because the cost of being wrong about that is
a false negative, and a false negative is caught by nobody (ADR-008).
"""

from __future__ import annotations

from collections.abc import Iterable

from pii_erasure.discovery.memory import ordered_candidates


def candidate_systems(priors: Iterable[str] = ()) -> tuple[str, ...]:
    """Every registered participant, ordered by relevance to the tenant's priors.

    Cold (no priors) this is registry order. Warm it is a permutation — same set,
    better sequence. Recall is identical either way, which is exactly the property
    `make eval` checks by running the gate both cold and warm (ADR-019).
    """
    return ordered_candidates(priors)
