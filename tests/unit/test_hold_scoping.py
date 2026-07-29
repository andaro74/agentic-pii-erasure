"""A hold blocks its scope, not the subject (ADR-027).

The decision M8's deployed gate forced. Four artifacts in this repo described holds as
scoped — `participants/_base/holds.py`, `seeds/meridian.json`, ARCHITECTURE §7.1, and the
`Deletability.PARTIAL` outcome — while `saga/nodes/hold_check.py` vetoed the whole subject
and called itself an M5 default. They disagreed for four milestones because the rule lived
in a package the saga could not import.

What these tests grade:

| Property | Why it matters |
|---|---|
| A scoped hold leaves the rest erasable | Over-retention is a violation, not a safe default |
| A subject-wide hold stays expressible | Scoping must not remove "freeze everything" |
| An unset scope covers everything | `"".startswith` matches all — by accident, until named |
| A scope matching nothing is reported | `scope: "all"` protects nothing, and looks like it does |

The last row is the one that would bite in the field. Under a subject-wide veto every
scope string was equivalent, so a mis-drafted one was invisible; now it is load-bearing.
"""

from __future__ import annotations

import pytest

from pii_erasure.contract import Artifact, Hold
from pii_erasure.contract.holds import (
    SUBJECT_WIDE_SCOPES,
    blocks,
    partition,
    unmatched_scopes,
)


def _hold(scope: str, hold_id: str = "hold-1") -> Hold:
    return Hold(hold_id=hold_id, authority="dc-court", scope=scope, basis="Art.17(3)(e)")


def _artifacts(*locators: str) -> tuple[Artifact, ...]:
    return tuple(Artifact(kind="row", locator=locator, count=1) for locator in locators)


# ─── scope, not subject ───────────────────────────────────────────────────────────────


def test_a_hold_covers_its_own_scope() -> None:
    assert blocks([_hold("public.invoices")], "public.invoices")


def test_a_hold_does_not_cover_a_sibling() -> None:
    """The whole decision in one assertion: a litigation hold over invoices gives no
    lawful basis to retain the subject's uploads."""
    assert not blocks([_hold("public.invoices")], "uploads/sub_a3f9/receipt.pdf")


def test_scope_matching_is_prefix_based() -> None:
    """Every store here names things hierarchically, and a scope drafted at the table
    level must cover the rows beneath it rather than nothing at all."""
    assert blocks([_hold("public.orders")], "public.orders.line_items")
    assert blocks([_hold("sub_a3f9/")], "sub_a3f9/photo.jpg")


def test_a_prefix_that_is_not_a_path_boundary_still_matches() -> None:
    """Documented rather than defended: `public.order` covers `public.orders`. Prefix
    matching is the conservative direction, and over-blocking is the safe error."""
    assert blocks([_hold("public.order")], "public.orders")


# ─── subject-wide must stay expressible ───────────────────────────────────────────────


@pytest.mark.parametrize("scope", sorted(SUBJECT_WIDE_SCOPES))
def test_a_subject_wide_scope_covers_everything(scope: str) -> None:
    """Making holds scoped must not make "a court froze everything" unsayable."""
    assert blocks([_hold(scope)], "anything/at/all")
    assert blocks([_hold(scope)], "public.invoices")


def test_an_unset_scope_is_treated_as_subject_wide_deliberately() -> None:
    """`"".startswith` is true of every string, so an unset scope already covered
    everything — by accident. Naming it makes the accident coincide with the safe
    reading instead of depending on a property of `str` nobody wrote down."""
    assert "" in SUBJECT_WIDE_SCOPES
    assert blocks([_hold("")], "public.invoices")


# ─── a scope that lands on nothing ────────────────────────────────────────────────────


def test_a_scope_matching_no_artifact_is_reported() -> None:
    """`scope: "all"` is a plausible thing for a human to write and matches only
    locators beginning with the letters "all"."""
    artifacts = _artifacts("public.invoices", "uploads/x.pdf")
    assert unmatched_scopes([_hold("all")], artifacts) == ("all",)


def test_a_scope_that_lands_is_not_reported() -> None:
    artifacts = _artifacts("public.invoices", "uploads/x.pdf")
    assert unmatched_scopes([_hold("public.invoices")], artifacts) == ()


def test_a_subject_wide_scope_is_never_reported_as_unmatched() -> None:
    """It matches everything by definition, so reporting it would be noise — and noise
    in this channel is how a real mis-drafted scope gets ignored."""
    assert unmatched_scopes([_hold("*")], _artifacts("x")) == ()
    assert unmatched_scopes([_hold("*")], ()) == ()


# ─── partition feeds both decisions ───────────────────────────────────────────────────


def test_partition_splits_held_from_actionable() -> None:
    artifacts = _artifacts("public.invoices", "public.orders", "uploads/x.pdf")
    held, actionable = partition(artifacts, [_hold("public.invoices")])
    assert [a.locator for a in held] == ["public.invoices"]
    assert [a.locator for a in actionable] == ["public.orders", "uploads/x.pdf"]


def test_partition_with_no_holds_leaves_everything_actionable() -> None:
    artifacts = _artifacts("a", "b")
    held, actionable = partition(artifacts, [])
    assert held == ()
    assert len(actionable) == 2


def test_partition_under_a_subject_wide_hold_leaves_nothing_actionable() -> None:
    """This is what still halts a saga: nothing to erase, so park it soft-deleted."""
    held, actionable = partition(_artifacts("a", "b"), [_hold("*")])
    assert len(held) == 2
    assert actionable == ()


def test_the_two_sides_always_account_for_every_artifact() -> None:
    """A partition that drops an artifact would under-report either what was retained or
    what remains erasable — and both directions are compliance failures."""
    artifacts = _artifacts("public.invoices", "public.orders", "uploads/x.pdf", "vec/1")
    held, actionable = partition(artifacts, [_hold("public.")])
    assert len(held) + len(actionable) == len(artifacts)
    assert set(held) | set(actionable) == set(artifacts)


# ─── the participant layer still sees the same rule ───────────────────────────────────


def test_the_participant_base_re_exports_the_same_function() -> None:
    """Two implementations of this rule is what caused the divergence. There is now one,
    and this asserts the participant layer did not keep a copy."""
    from pii_erasure.participants._base import holds as participant_holds

    assert participant_holds.blocks is blocks
