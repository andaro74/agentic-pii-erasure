"""Idempotency keys (§4.3) — the defence against replay in a system with no compensation.

Phase 3 never compensates (invariant 6), so a duplicate `hard_delete` cannot be undone
by rolling back; it can only be prevented. Lambda retries, at-least-once Scheduler
delivery, and checkpoint resume all replay calls, which makes this key correctness rather
than politeness.
"""

from __future__ import annotations

import pytest

from pii_erasure.contract import Artifact, Verb, idempotency_key
from pii_erasure.contract.idempotency import IdempotencyKeyError

_A = Artifact(kind="row", locator="public.orders", count=412)
_B = Artifact(kind="row", locator="public.invoices", count=8)


def _key(**overrides: object) -> str:
    payload: dict[str, object] = {
        "saga_id": "saga_01JQ8",
        "system_id": "billing-ledger",
        "operation": Verb.HARD_DELETE,
        "artifacts": [_A, _B],
    }
    payload.update(overrides)
    return idempotency_key(**payload)  # type: ignore[arg-type]


def test_the_same_work_produces_the_same_key() -> None:
    assert _key() == _key()
    assert _key().startswith("sha256:")


def test_artifact_order_does_not_change_the_key() -> None:
    """Discovery re-run against a paginated API returns the same artifacts in a different
    order. If that produced a new key, every retry would delete twice."""
    assert _key(artifacts=[_A, _B]) == _key(artifacts=[_B, _A])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("saga_id", "saga_other"),
        ("system_id", "profile-store"),
        ("operation", Verb.SOFT_DELETE),
        ("artifacts", [_A]),
    ],
)
def test_every_component_changes_the_key(field: str, value: object) -> None:
    assert _key(**{field: value}) != _key()


def test_concatenation_cannot_be_made_ambiguous() -> None:
    """Plain concatenation would make these two collide, so a `hard_delete` in one saga
    would return ALREADY_APPLIED against the other's record having deleted nothing."""
    left = idempotency_key(saga_id="sag", system_id="a", operation=Verb.HARD_DELETE, artifacts=[])
    right = idempotency_key(saga_id="sa", system_id="ga", operation=Verb.HARD_DELETE, artifacts=[])
    assert left != right


@pytest.mark.parametrize("field", ["saga_id", "system_id"])
def test_empty_components_are_refused(field: str) -> None:
    with pytest.raises(IdempotencyKeyError):
        _key(**{field: ""})


@pytest.mark.parametrize("field", ["saga_id", "system_id"])
def test_a_nul_in_a_component_is_refused(field: str) -> None:
    """The separator must stay unambiguous — a caller that could inject one could forge
    a collision deliberately."""
    with pytest.raises(IdempotencyKeyError):
        _key(**{field: "saga\x00id"})


def test_an_empty_artifact_set_still_produces_a_key() -> None:
    """A no-op delete is still a call worth de-duplicating."""
    assert _key(artifacts=[]).startswith("sha256:")
    assert _key(artifacts=[]) != _key()
