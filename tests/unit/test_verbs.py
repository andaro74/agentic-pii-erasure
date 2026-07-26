"""The five-verb contract as types — including the invariants it makes unrepresentable.

Two of these tests are the reason the models carry validators at all: invariant 7
(`APPLIED` with outstanding work) and the requirement that `hard_delete` cannot be
constructed without a digest-bound approval token. Both are enforced elsewhere too —
by Cedar at the Gateway, and by the participant re-validating in-process — and that is
the design: three independent places, none of them weakened because the others exist.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pii_erasure.contract import (
    MUTATING_VERBS,
    READ_ONLY_VERBS,
    Archetype,
    Artifact,
    Deletability,
    DiscoverRequest,
    DiscoverResponse,
    DiscoveryEvidence,
    HardDeleteRequest,
    Hold,
    MutationResponse,
    Outcome,
    ReceiptEvidence,
    Residual,
    SoftDeleteRequest,
    Verb,
    VerifyResponse,
    canonical,
)

_EVIDENCE = DiscoveryEvidence(query_digest="sha256:abc", observed_at="2026-07-23T10:14:02Z")
_RECEIPT = ReceiptEvidence(receipt_digest="sha256:def", applied_at="2026-07-23T10:15:00Z")
_ARTIFACT = Artifact(kind="row", locator="public.orders", count=412, classification=("PII",))


def _mutation(**overrides: object) -> MutationResponse:
    payload: dict[str, object] = {
        "system_id": "profile-store",
        "outcome": Outcome.APPLIED,
        "affected": 1,
        "evidence": _RECEIPT,
    }
    payload.update(overrides)
    return MutationResponse(**payload)  # type: ignore[arg-type]


# ─── Invariant 7 · residual honesty ───────────────────────────────────────────────────


def test_applied_with_a_residual_cannot_be_constructed() -> None:
    residual = Residual(kind="hash", locator="ses:suppression", reason="legally retained")
    with pytest.raises(ValidationError) as raised:
        _mutation(outcome=Outcome.APPLIED, residual=(residual,))
    assert "invariant 7" in str(raised.value)


def test_partial_without_a_residual_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        _mutation(outcome=Outcome.PARTIAL)


def test_the_honest_partial_is_representable() -> None:
    """`notify-suppression`'s real shape: the SES suppression hash stays, and says so."""
    response = _mutation(
        outcome=Outcome.PARTIAL,
        residual=(Residual(kind="hash", locator="ses:suppression", reason="required by SES"),),
    )
    assert response.outcome is Outcome.PARTIAL
    assert response.residual[0].reason


@pytest.mark.parametrize("outcome", [Outcome.REFUSED, Outcome.ALREADY_APPLIED])
def test_no_other_outcome_may_carry_a_residual(outcome: Outcome) -> None:
    with pytest.raises(ValidationError):
        _mutation(
            outcome=outcome,
            residual=(Residual(kind="row", locator="t", reason="whatever"),),
        )


# ─── Approval binding is structural, not procedural ───────────────────────────────────


def test_hard_delete_without_an_approval_token_does_not_construct() -> None:
    common = {
        "subject_ref": "sub_a3f9",
        "saga_id": "saga_1",
        "manifest_digest": "sha256:abc",
        "idempotency_key": "sha256:def",
        "artifacts": (_ARTIFACT,),
    }
    SoftDeleteRequest(**common)  # type: ignore[arg-type]  — needs no approval
    with pytest.raises(ValidationError):
        HardDeleteRequest(**common)  # type: ignore[arg-type]


def test_a_mutation_always_carries_a_manifest_digest() -> None:
    with pytest.raises(ValidationError):
        SoftDeleteRequest(  # type: ignore[call-arg]
            subject_ref="sub_a3f9",
            saga_id="saga_1",
            idempotency_key="sha256:def",
            artifacts=(_ARTIFACT,),
        )


def test_an_empty_digest_is_not_a_digest() -> None:
    with pytest.raises(ValidationError):
        SoftDeleteRequest(
            subject_ref="sub_a3f9",
            saga_id="saga_1",
            manifest_digest="",
            idempotency_key="sha256:def",
            artifacts=(_ARTIFACT,),
        )


# ─── Discovery responses cannot quietly disagree with themselves ──────────────────────


def test_found_without_artifacts_is_refused() -> None:
    with pytest.raises(ValidationError):
        DiscoverResponse(
            system_id="profile-store",
            archetype=Archetype.OPERATIONAL_NOSQL,
            found=True,
            deletability=Deletability.DELETABLE,
            evidence=_EVIDENCE,
        )


def test_artifacts_without_found_is_refused() -> None:
    """A false negative is caught by nobody — recall failures start as small
    inconsistencies like this one (invariant 8)."""
    with pytest.raises(ValidationError):
        DiscoverResponse(
            system_id="profile-store",
            archetype=Archetype.OPERATIONAL_NOSQL,
            found=False,
            deletability=Deletability.NOT_PRESENT,
            evidence=_EVIDENCE,
            artifacts=(_ARTIFACT,),
        )


def test_a_hold_must_be_reflected_in_the_assessment() -> None:
    hold = Hold(
        hold_id="LIT-2024-118",
        authority="Legal",
        scope="public.orders",
        basis="Art.17(3)(e)",
    )
    with pytest.raises(ValidationError):
        DiscoverResponse(
            system_id="billing-ledger",
            archetype=Archetype.RELATIONAL,
            found=True,
            deletability=Deletability.DELETABLE,
            evidence=_EVIDENCE,
            artifacts=(_ARTIFACT,),
            holds=(hold,),
        )


def test_verify_cannot_claim_clean_with_remains() -> None:
    with pytest.raises(ValidationError):
        VerifyResponse(
            system_id="upload-bucket",
            clean=True,
            evidence=_EVIDENCE,
            remaining=(_ARTIFACT,),
        )


# ─── Wire shape ───────────────────────────────────────────────────────────────────────


def test_the_wire_format_is_camel_case() -> None:
    request = DiscoverRequest(subject_ref="sub_a3f9", saga_id="saga_1", scope_hints=("billing",))
    assert request.digested_body() == {
        "subjectRef": "sub_a3f9",
        "sagaId": "saga_1",
        "scopeHints": ["billing"],
    }


def test_a_request_can_be_parsed_from_the_wire_names() -> None:
    request = DiscoverRequest.model_validate({"subjectRef": "sub_a3f9", "sagaId": "saga_1"})
    assert request.subject_ref == "sub_a3f9"


def test_unknown_fields_are_rejected_rather_than_ignored() -> None:
    """A participant response is parsed from a Lambda the agent does not control."""
    with pytest.raises(ValidationError):
        DiscoverRequest.model_validate(
            {"subjectRef": "sub_a3f9", "sagaId": "saga_1", "adminOverride": True}
        )


def test_requests_are_frozen() -> None:
    request = DiscoverRequest(subject_ref="sub_a3f9", saga_id="saga_1")
    with pytest.raises(ValidationError):
        request.subject_ref = "sub_other"  # type: ignore[misc]


def test_a_body_is_canonicalisable_and_order_independent() -> None:
    """The join between this module and canonical.py: what the models emit must be what
    the canonicaliser accepts, artifact order included."""
    forward = SoftDeleteRequest(
        subject_ref="sub_a3f9",
        saga_id="saga_1",
        manifest_digest="sha256:abc",
        idempotency_key="sha256:def",
        artifacts=(_ARTIFACT, Artifact(kind="row", locator="public.invoices")),
    )
    reverse = SoftDeleteRequest(
        subject_ref="sub_a3f9",
        saga_id="saga_1",
        manifest_digest="sha256:abc",
        idempotency_key="sha256:def",
        artifacts=(Artifact(kind="row", locator="public.invoices"), _ARTIFACT),
    )
    assert canonical(forward.digested_body()) == canonical(reverse.digested_body())


def test_evidence_cannot_be_smuggled_into_a_digested_body() -> None:
    """Evidence carries `observedAt`, and canonical.py refuses it — which is how
    provenance stays out of the digest rather than by everyone remembering to strip it."""
    from pii_erasure.contract.canonical import CanonicalisationError

    with pytest.raises(CanonicalisationError):
        canonical({"evidence": _EVIDENCE.digested_body()})


# ─── The verb surface itself ──────────────────────────────────────────────────────────


def test_the_five_verbs_are_exactly_five() -> None:
    assert len(list(Verb)) == 5
    assert set(Verb) == READ_ONLY_VERBS | MUTATING_VERBS
    assert not READ_ONLY_VERBS & MUTATING_VERBS


def test_no_mutating_verb_is_read_only() -> None:
    """Invariant 1's vocabulary. The discovery subgraph is built from READ_ONLY_VERBS,
    so a mutating verb leaking into that set would hand a model a deletion tool."""
    assert Verb.HARD_DELETE not in READ_ONLY_VERBS
    assert Verb.SOFT_DELETE not in READ_ONLY_VERBS
    assert Verb.RESTORE not in READ_ONLY_VERBS
    assert {Verb.DISCOVER, Verb.VERIFY} == READ_ONLY_VERBS
