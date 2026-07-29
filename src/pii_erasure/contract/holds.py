"""Hold scope matching — the one rule both planes must agree on (ADR-027).

This lived in `participants/_base/holds.py`, where it was correct and where the saga
could not reach it without importing the participants package — a dependency direction
this repo does not have. So the saga grew its own answer, and the two disagreed for four
milestones: the participant scoped per artifact, the saga vetoed the whole subject.

Moving the rule into `contract/` is the fix that makes the disagreement unrepresentable.
`contract/` depends on nothing and everything depends on it, which is exactly the shape a
shared rule needs. `participants/_base/holds.py` re-exports `blocks` so its own callers
are unchanged.

**A hold blocks a scope, not a subject.** A litigation hold over `public.orders` gives no
lawful basis to retain the subject's uploads (Art. 17(3)(e) exempts what is needed for
legal claims, and nothing more), so treating it as subject-wide silently over-retains —
an under-deletion with no error attached, which is the failure recall cannot see.
"""

from __future__ import annotations

from collections.abc import Sequence

from pii_erasure.contract.verbs import Artifact, Hold

#: Scopes that mean *the whole subject*. Making holds scoped would otherwise make a
#: subject-wide hold **inexpressible** — a court freezing everything is a real and
#: necessary case, and ADR-027 must not remove the ability to say it.
#:
#: The empty string is included on purpose, and it is the safety-critical half. Prefix
#: matching makes `"".startswith` true of every locator, so an unset scope would cover
#: everything *by accident*. Naming it here makes the accident coincide with the safe
#: reading rather than depending on a property of `str.startswith` nobody stated.
SUBJECT_WIDE_SCOPES = frozenset({"*", ""})


def blocks(holds: Sequence[Hold], locator: str) -> bool:
    """Does any hold cover `locator`?

    Scope matching is **prefix-based**, which is how every store here names things
    hierarchically — `public.orders` covers `public.orders.line_items`, and `sub_a3f9/`
    covers every object beneath it.

    Prefix rather than exact match is the conservative direction, and deliberately so:
    ADR-027 moves real weight onto the scope string, so a scope drafted at the table
    level must cover the rows beneath it rather than nothing at all.

    **A scope that names nothing real covers nothing.** `scope="all"` is a plausible
    thing to write and matches only locators beginning with the letters `all` — which is
    why `SUBJECT_WIDE_SCOPES` exists and why `unmatched_scopes` is available to callers
    that want to surface the mistake rather than under-block silently.
    """
    return any(
        hold.scope in SUBJECT_WIDE_SCOPES or locator == hold.scope or locator.startswith(hold.scope)
        for hold in holds
    )


def unmatched_scopes(holds: Sequence[Hold], artifacts: Sequence[Artifact]) -> tuple[str, ...]:
    """Hold scopes that cover none of these artifacts.

    A hold that matches nothing is far more likely to be a mis-drafted scope than a hold
    over data that does not exist — `scope="all"` being the worked example. ADR-027 puts
    real weight on this string, so a caller can ask whether it landed instead of assuming
    it did. Returns scopes, not hold ids, because the scope is the thing to fix.
    """
    return tuple(
        hold.scope
        for hold in holds
        if hold.scope not in SUBJECT_WIDE_SCOPES
        and not any(blocks([hold], artifact.locator) for artifact in artifacts)
    )


def partition(
    artifacts: Sequence[Artifact], holds: Sequence[Hold]
) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
    """Split artifacts into (held, actionable).

    Returned as two tuples rather than a predicate because both sides are needed: the
    actionable set decides whether the saga proceeds, and the held set is what gets
    disclosed as residual risk (invariant 7). A caller that only asked "is anything
    held?" would have to walk the list twice and would be tempted to report neither.
    """
    held = tuple(artifact for artifact in artifacts if blocks(holds, artifact.locator))
    actionable = tuple(artifact for artifact in artifacts if not blocks(holds, artifact.locator))
    return held, actionable
