"""The approval view is a control (ARCHITECTURE §8.4, ROADMAP M8's trap).

The threat here is not a bug. It is a bored human. T2 makes approval mandatory for every
hard delete, so an operator sees this screen constantly, and the failure mode is that they
stop reading it — at which point the ledger records a review that did not happen, which is
worse than having no gate at all.

So these tests grade the properties that survive an approver who reads only the top of the
screen:

| Property | Why it is a control |
|---|---|
| Anomalies and residual risk precede inventory | The reverse buries the finding under 400 rows |
| The inventory is bounded, and says what it omitted | A list that just stops is a lie by omission |
| No baseline yields a *finding*, never silence | "Nothing unusual" misreads "not compared" |
| A missing system is an anomaly | The recall smell a human can catch and a gate cannot |
| The view carries no raw PII | Invariant 5 reaches the approver's screen too |

What is deliberately NOT tested is whether the anomalies are the *right* ones in some
absolute sense — that is a judgement call this module makes visible rather than makes.
"""

from __future__ import annotations

from typing import Any

import pytest

from pii_erasure.approval.presenter import (
    EXPECTED_SYSTEM_SHARE,
    INVENTORY_LIMIT,
    MIN_DELETIONS_FOR_BASELINE,
    SECTION_ORDER,
    TenantBaseline,
    anomalies,
    baseline_from_history,
    present,
    render_text,
    required_tier,
)
from pii_erasure.contract.archetypes import Archetype
from pii_erasure.contract.verbs import Artifact, Hold, Residual
from pii_erasure.manifest.models import Manifest, ManifestParticipant, OrderSlot, Provenance


def _participant(
    system_id: str = "profile-store",
    *,
    artifacts: tuple[Artifact, ...] | None = None,
    delete_method: str | None = "PURGE",
    archetype: Archetype = Archetype.OPERATIONAL_NOSQL,
) -> ManifestParticipant:
    return ManifestParticipant(
        system_id=system_id,
        archetype=archetype,
        artifacts=artifacts or (Artifact(kind="row", locator=f"{system_id}#1", count=1),),
        planned_ops=("soft_delete", "hard_delete"),
        order=OrderSlot(phase=2, rank=0),
        delete_method=delete_method,
        # The model refuses a CRYPTO_SHRED that names no shred target, which is the right
        # refusal — a shred with no DEK reference deletes nothing while reporting success.
        dek_registry_ref="dek/sub_1" if delete_method == "CRYPTO_SHRED" else None,
    )


def _manifest(
    *,
    participants: tuple[ManifestParticipant, ...] | None = None,
    residual_risk: tuple[Residual, ...] = (),
    legal_holds: tuple[Hold, ...] = (),
    grace_window_days: int = 30,
) -> Manifest:
    return Manifest(
        manifest_id="man_1",
        saga_id="saga_1",
        subject_ref="sub_1",
        request_id="req_1",
        provenance=Provenance(discovered_at="2026-07-28T00:00:00Z", agent_version="test@1"),
        participants=participants or (_participant(),),
        residual_risk=residual_risk,
        legal_holds=legal_holds,
        grace_window_days=grace_window_days,
        digest="sha256:abc",
    )


def _rich_baseline(systems: tuple[str, ...] = ("profile-store",), n: int = 40) -> TenantBaseline:
    return TenantBaseline(deletions_observed=n, systems_seen=dict.fromkeys(systems, n))


# ─── 1. ordering is the control ───────────────────────────────────────────────────────


def test_anomalies_and_residual_risk_precede_the_inventory() -> None:
    """The property the whole module exists for. If this reverses, the gate is theatre."""
    view = present(_manifest(), baseline=_rich_baseline())
    order = view["sections"]
    assert order.index("residualRisk") < order.index("inventory")
    assert order.index("anomalies") < order.index("inventory")


def test_the_inventory_is_last() -> None:
    assert SECTION_ORDER[-1] == "inventory"
    assert SECTION_ORDER[0] == "residualRisk"


def test_the_rendered_text_follows_the_same_order() -> None:
    """The CLI and the API must not disagree about what an approver sees first — one of
    them being anomaly-first is not a control if the other one isn't."""
    text = render_text(present(_manifest(), baseline=_rich_baseline()))
    positions = [text.index(f"── {section} ──") for section in SECTION_ORDER]
    assert positions == sorted(positions)


def test_a_reordered_section_list_would_be_caught() -> None:
    """Guard against a vacuous ordering test: the assertions above compare indices, so
    they must actually move if the constant changes."""
    assert list(SECTION_ORDER) != sorted(SECTION_ORDER), (
        "SECTION_ORDER happens to be alphabetical, so an index comparison proves nothing"
    )


# ─── 2. the inventory is bounded and honest about it ──────────────────────────────────


def test_four_hundred_rows_do_not_reach_the_approver() -> None:
    """§8.4's exact wording: an approval UI that dumps 400 JSON artifacts guarantees
    rubber-stamping."""
    many = tuple(Artifact(kind="row", locator=f"row#{i}", count=1) for i in range(400))
    view = present(_manifest(participants=(_participant(artifacts=many),)))
    assert len(view["inventory"]["shown"]) == INVENTORY_LIMIT


def test_omitted_rows_are_counted_not_dropped() -> None:
    """A truncated list that does not say it was truncated reads as a complete one."""
    many = tuple(Artifact(kind="row", locator=f"row#{i}", count=1) for i in range(400))
    view = present(_manifest(participants=(_participant(artifacts=many),)))
    assert view["inventory"]["totalRows"] == 400
    assert view["inventory"]["omitted"] == 400 - INVENTORY_LIMIT
    assert "380 more" in render_text(view)


def test_a_small_plan_omits_nothing() -> None:
    view = present(_manifest(), baseline=_rich_baseline())
    assert view["inventory"]["omitted"] == 0
    assert "more row" not in render_text(view)


# ─── 3. an absent baseline is a finding, not silence ──────────────────────────────────


def test_no_baseline_produces_an_anomaly_rather_than_an_empty_list() -> None:
    """The tempting behaviour — no history, no anomalies — reads to a tired approver as
    "nothing unusual here". It is the opposite: nothing was compared."""
    view = present(_manifest(), baseline=None)
    kinds = {a["kind"] for a in view["anomalies"]}
    assert "baseline-unavailable" in kinds


def test_a_thin_history_is_also_unusable() -> None:
    thin = TenantBaseline(
        deletions_observed=MIN_DELETIONS_FOR_BASELINE - 1, systems_seen={"profile-store": 1}
    )
    kinds = {a.kind for a in anomalies(_manifest(), thin)}
    assert "baseline-unavailable" in kinds
    assert "unseen-system" not in kinds, "a thin baseline must not also cry wolf"


def test_a_usable_baseline_produces_no_baseline_warning() -> None:
    kinds = {a.kind for a in anomalies(_manifest(), _rich_baseline())}
    assert "baseline-unavailable" not in kinds


# ─── 4. the two directions of drift ───────────────────────────────────────────────────


def test_a_system_the_tenant_has_never_touched_is_flagged() -> None:
    """§8.4's worked example: "this deletion touches a system the last 40 deletions did
    not"."""
    manifest = _manifest(participants=(_participant(), _participant("billing-ledger")))
    found = anomalies(manifest, _rich_baseline(("profile-store",), n=40))
    unseen = [a for a in found if a.kind == "unseen-system"]
    assert [a.system_id for a in unseen] == ["billing-ledger"]
    assert "40" in unseen[0].detail


def test_a_system_the_tenant_always_has_but_this_plan_lacks_is_flagged() -> None:
    """The direction that catches a *recall* failure. A gate cannot see this — the plan
    is internally consistent — but a human who deletes from this tenant weekly can."""
    baseline = TenantBaseline(
        deletions_observed=40, systems_seen={"profile-store": 40, "vector-index": 40}
    )
    found = anomalies(_manifest(), baseline)
    missing = [a for a in found if a.kind == "missing-system"]
    assert [a.system_id for a in missing] == ["vector-index"]


def test_an_occasional_system_is_not_flagged_as_missing() -> None:
    """Below the share threshold this is normal variation, and an approver who learns to
    dismiss this row learns to dismiss the one above it."""
    occasional = int(40 * EXPECTED_SYSTEM_SHARE) - 1
    baseline = TenantBaseline(
        deletions_observed=40, systems_seen={"profile-store": 40, "analytics-lake": occasional}
    )
    assert not [a for a in anomalies(_manifest(), baseline) if a.kind == "missing-system"]


def test_high_severity_findings_sort_above_medium() -> None:
    manifest = _manifest(
        participants=(_participant("compliance-archive", delete_method="CRYPTO_SHRED"),),
        residual_risk=(Residual(kind="row", locator="lake#1", reason="snapshot window"),),
    )
    found = anomalies(manifest, _rich_baseline(("compliance-archive",)))
    severities = [a.severity for a in found]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)


# ─── 5. irreversibility and tier ──────────────────────────────────────────────────────


def test_crypto_shred_is_called_out_as_unrecoverable() -> None:
    manifest = _manifest(
        participants=(_participant("compliance-archive", delete_method="CRYPTO_SHRED"),)
    )
    view = present(manifest, baseline=_rich_baseline(("compliance-archive",)))
    assert view["irreversibility"]["cryptoShredSystems"] == ["compliance-archive"]
    assert any(a["kind"] == "crypto-shred" for a in view["anomalies"])


def test_the_grace_window_is_shown_because_it_is_the_countdown() -> None:
    view = present(_manifest(grace_window_days=7), baseline=_rich_baseline())
    assert view["irreversibility"]["graceWindowDays"] == 7


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "T2"),
        ({"residual_risk": (Residual(kind="row", locator="x", reason="kept"),)}, "T3"),
        (
            {"legal_holds": (Hold(hold_id="LIT-1", authority="court", scope="all", basis="e"),)},
            "T3",
        ),
        (
            {"participants": (_participant("compliance-archive", delete_method="CRYPTO_SHRED"),)},
            "T3",
        ),
    ],
    ids=["plain-hard-delete", "residual-risk", "legal-hold", "crypto-shred"],
)
def test_the_tier_matches_the_risk_table(kwargs: dict[str, Any], expected: str) -> None:
    """§8.1's table. Computed once here so the API and the CLI cannot disagree about
    whether a plan needs two people."""
    assert required_tier(_manifest(**kwargs)) == expected


def test_a_legal_hold_says_it_is_rechecked_later() -> None:
    """An approver must not read "hold present" as "hold already handled". Holds veto at
    the grace-window re-check regardless of what was approved here."""
    hold = Hold(hold_id="LIT-9", authority="court", scope="billing", basis="Art.17(3)(e)")
    found = anomalies(_manifest(legal_holds=(hold,)), _rich_baseline())
    detail = next(a.detail for a in found if a.kind == "legal-hold")
    assert "re-checked" in detail


# ─── 6. invariant 5 reaches the approver's screen ─────────────────────────────────────


def test_an_email_leaking_from_a_participant_never_reaches_the_view() -> None:
    """The manifest should carry no PII at all — participants return locators and counts.
    This is the backstop for one that misbehaves, and it matters because the approval view
    is copied into a browser, a ticket, and the ledger entry recording the review."""
    leaky = _participant(artifacts=(Artifact(kind="user", locator="ada@example.invalid"),))
    view = present(_manifest(participants=(leaky,)))
    assert "ada@example.invalid" not in str(view)
    assert "[REDACTED]" in str(view)


def test_the_subject_handle_survives_scrubbing() -> None:
    """A scrubber that eats the correlation key is as useless as one that leaks."""
    view = present(_manifest(), baseline=_rich_baseline())
    assert view["subjectRef"] == "sub_1"
    assert view["sagaId"] == "saga_1"


# ─── 7. building a baseline from history ──────────────────────────────────────────────


def test_history_folds_into_counts() -> None:
    history = [
        {"systems": ["profile-store", "vector-index"]},
        {"systems": ["profile-store"]},
        {"systems": ["profile-store", "vector-index"]},
    ]
    baseline = baseline_from_history(history)
    assert baseline.deletions_observed == 3
    assert baseline.systems_seen == {"profile-store": 3, "vector-index": 2}


def test_a_system_listed_twice_in_one_deletion_counts_once() -> None:
    """Otherwise one manifest with two participants on the same system inflates the
    denominator's numerator and a share can exceed 1.0."""
    baseline = baseline_from_history([{"systems": ["profile-store", "profile-store"]}])
    assert baseline.systems_seen == {"profile-store": 1}
    assert baseline.share("profile-store") == 1.0


def test_an_empty_history_is_unusable_rather_than_perfect() -> None:
    assert not baseline_from_history([]).is_usable


def test_the_digest_the_approver_echoes_is_the_digest_that_was_signed() -> None:
    """The whole approval binding, end to end through the view (V11-6).

    `present()` scrubs its output, and the scrubber's phone rule used to eat digit runs
    inside the hex digest. The operator then echoed a `[REDACTED]`-spliced digest, the API
    compared it against the real one, and a legitimate approval was refused as a changed
    plan — the control firing correctly on corrupted input, which is the hardest kind of
    bug to read from the error message.
    """
    import hashlib

    for i in range(50):
        digest = f"sha256:{hashlib.sha256(str(i).encode()).hexdigest()}"
        manifest = _manifest()
        manifest = manifest.model_copy(update={"digest": digest})
        view = present(manifest, baseline=_rich_baseline())
        assert view["manifestDigest"] == digest, "the view corrupted the approval binding"
