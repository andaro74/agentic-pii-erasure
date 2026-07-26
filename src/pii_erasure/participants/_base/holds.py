"""Shared hold evaluation.

A legal hold is the one thing that legitimately beats an erasure request (GDPR
Art. 17(3)(e)). Two properties matter and are easy to get wrong:

* **A hold blocks a scope, not a subject.** A litigation hold over `public.orders` does
  not protect the subject's uploads. Treating it as subject-wide would silently
  under-delete, which is a recall failure wearing a compliance costume.
* **A hold is re-checked, never remembered.** Phase 3 re-evaluates holds after the grace
  window (§5.3) because one can appear *during* it. This module is therefore pure — it
  takes the holds observed right now and answers about them, and holds nothing itself.
"""

from __future__ import annotations

from collections.abc import Sequence

from pii_erasure.contract import Artifact, Deletability, Hold


def blocks(holds: Sequence[Hold], locator: str) -> bool:
    """Does any hold cover `locator`?

    Scope matching is prefix-based, which is how every store here names things
    hierarchically — `public.orders` covers `public.orders.line_items`, and `sub_a3f9/`
    covers every object beneath it.
    """
    return any(locator == hold.scope or locator.startswith(hold.scope) for hold in holds)


def deletability(
    artifacts: Sequence[Artifact],
    holds: Sequence[Hold],
    *,
    undeletable_kinds: frozenset[str] = frozenset(),
) -> Deletability:
    """Classify what this participant can actually do, from what it can actually see.

    `undeletable_kinds` is how an archetype declares that some of what it found can never
    be removed — the WORM ciphertext, the SES suppression entry. Reporting those as
    `DELETABLE` and discovering otherwise at phase 3 is the failure invariant 7 exists to
    prevent, and it starts here, at plan time, not at execution.
    """
    if not artifacts:
        return Deletability.NOT_PRESENT

    held = [artifact for artifact in artifacts if blocks(holds, artifact.locator)]
    if held and len(held) == len(artifacts):
        return Deletability.BLOCKED_BY_HOLD
    if held:
        return Deletability.PARTIAL
    if any(artifact.kind in undeletable_kinds for artifact in artifacts):
        return Deletability.PARTIAL
    return Deletability.DELETABLE
