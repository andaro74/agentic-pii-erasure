"""Invariant 13 — AgentCore Memory holds topology, never subject data.

Memory is a cross-subject surface: something learned deleting subject A is retrieved
while deleting subject B. That is its value and its whole danger. A leak here crosses
the exact boundary the architecture protects (threat T7) and it leaks *silently* —
nothing fails, a later run simply retrieves a fact about someone else's data.

The tests are organised around the gap that makes this hard: **content scrubbing is not
enough.** `sub_a3f9c1` contains no PII, passes every email/phone rule, and is precisely
the thing that must never be stored. So the shape rules are tested first and hardest.

The second axis is ADR-019's other promise — priors are advisory. `ordered_candidates`
is asserted to be a *permutation*: same members, possibly different order. A prior that
could shorten the sweep would turn a performance hint into a recall failure, and recall
failures are caught by nobody (ADR-008).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from pii_erasure.contract.registry import system_ids
from pii_erasure.discovery.memory import (
    MemoryWriteRejectedError,
    Prior,
    TopologyMemory,
    assert_topology_only,
    ordered_candidates,
)

TENANT = "meridian"


class RecordingClient:
    """Captures what would have been written, so a rejected write is provably absent."""

    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.written: list[dict[str, Any]] = []
        self._stored = records or []

    def batch_create_memory_records(self, **kwargs: Any) -> dict[str, Any]:
        self.written.extend(kwargs["records"])
        return {}

    def retrieve_memory_records(self, **_kwargs: Any) -> dict[str, Any]:
        return {"memoryRecordSummaries": self._stored}

    def list_memory_records(self, **_kwargs: Any) -> dict[str, Any]:
        return {"memoryRecordSummaries": self._stored}


def _memory(client: Any) -> TopologyMemory:
    return TopologyMemory(memory_id="mem-1", client=client, tenant=TENANT)


# ─── 1. what may never be written ─────────────────────────────────────────────────────

#: Each entry is (rule, text). These are the shapes that pass a PII content scan while
#: being exactly what invariant 13 forbids — the reason this module exists.
FORBIDDEN = [
    ("subject_handle", "tenant meridian holds data in profile-store for sub_a3f9c1d2"),
    ("subject_handle", "SUB-9F3A21 was found in vector-index"),
    ("saga_id", "during saga_01jq8xyz the lake held rows"),
    ("manifest_id", "man_01jq8abc listed eight participants"),
    ("digest", "approved under sha256:deadbeefcafe0123"),
    ("hold_id", "LIT-9999 blocks the orders table"),
    ("arn", "found at arn:aws:s3:::asdp-dev-uploads/x"),
]


@pytest.mark.parametrize(("rule", "text"), FORBIDDEN, ids=[r for r, _ in FORBIDDEN])
def test_a_subject_shaped_prior_is_rejected(rule: str, text: str) -> None:
    with pytest.raises(MemoryWriteRejectedError) as caught:
        assert_topology_only(text)
    assert caught.value.rule == rule


def test_raw_pii_is_rejected_too() -> None:
    """The content layer still runs. Belt and braces, in that order."""
    with pytest.raises(MemoryWriteRejectedError) as caught:
        assert_topology_only("contact the tenant admin at ops@meridian.example")
    assert caught.value.rule == "email"


def test_the_rejection_never_echoes_the_offending_value() -> None:
    """This exception's message ends up in logs — which is the other place the value
    must not appear (invariant 5)."""
    with pytest.raises(MemoryWriteRejectedError) as caught:
        assert_topology_only("sub_a3f9c1d2 lives in profile-store")
    assert "sub_a3f9c1d2" not in str(caught.value)


@pytest.mark.parametrize(("_rule", "text"), FORBIDDEN, ids=[r for r, _ in FORBIDDEN])
def test_a_rejected_batch_reaches_no_client_at_all(_rule: str, text: str) -> None:
    """Rejection must happen before the API call, not be cleaned up after it.

    Also pins whole-batch rejection: a clean prior sits alongside the suspect one, and
    neither is written. Dropping the bad record and storing the rest would make a leak
    attempt indistinguishable from a clean write in the return value.
    """
    client = RecordingClient()
    priors = [Prior(TENANT, "vector-index mirrors profile-store"), Prior(TENANT, text)]
    with pytest.raises(MemoryWriteRejectedError):
        _memory(client).write(priors)
    assert client.written == [], "a suspect batch reached the client"


# ─── 2. what topology legitimately looks like ────────────────────────────────────────

PERMITTED = [
    "this tenant holds subject data in profile-store, billing-ledger and analytics-lake",
    "vector-index is derived from profile-store and must be purged first",
    "scope hint 'customer_email_lower' has been productive for this tenant",
    "compliance-archive consistently returns found=false for this tenant",
]


@pytest.mark.parametrize("text", PERMITTED)
def test_topology_facts_are_allowed(text: str) -> None:
    assert assert_topology_only(text) == text


def test_a_clean_batch_is_written_with_the_tenant_namespace() -> None:
    text = PERMITTED[0]
    client = RecordingClient()
    stamp = datetime(2026, 7, 27, tzinfo=timezone.utc)
    written = _memory(client).write([Prior(TENANT, text)], now=stamp)
    assert written == 1
    record = client.written[0]
    assert record["namespaces"] == [f"/topology/{TENANT}"]
    assert record["content"]["text"] == text
    assert "subject" not in " ".join(record["namespaces"]).lower()


def test_an_empty_batch_is_a_no_op() -> None:
    client = RecordingClient()
    assert _memory(client).write([]) == 0
    assert client.written == []


def test_a_cold_tenant_reads_no_priors() -> None:
    """The normal first run. Absence is not an error."""
    assert _memory(RecordingClient([])).read() == []


def test_all_records_is_what_the_evaluator_reads_back() -> None:
    """`no_pii_in_memory` must see everything, not a semantic sample — an evaluator
    that grades a subset reports a certainty it does not have."""
    stored = [{"content": {"text": t}} for t in PERMITTED]
    assert set(_memory(RecordingClient(stored)).all_records()) == set(PERMITTED)


# ─── 3. priors are advisory — they reorder, never shorten ────────────────────────────


def test_priors_reorder_the_sweep() -> None:
    ordered = ordered_candidates(["vector-index mirrors profile-store"])
    assert ordered[0] == "vector-index"
    assert ordered[1] == "profile-store"


@pytest.mark.parametrize(
    "priors",
    [
        (),
        ("vector-index mirrors profile-store",),
        ("this tenant has no billing-ledger and no upload-bucket",),
        ("compliance-archive always returns found=false — skip it",),
        ("analytics-lake",),
    ],
    ids=["cold", "reorder", "claims-absence", "says-skip", "single"],
)
def test_no_prior_can_shorten_the_sweep(priors: tuple[str, ...]) -> None:
    """ADR-019's load-bearing promise. The `says-skip` and `claims-absence` cases are
    the ones that matter: a prior asserting a system is empty must still leave that
    system in the sweep, because being wrong about it is a false negative."""
    assert set(ordered_candidates(priors)) == set(system_ids())
    assert len(ordered_candidates(priors)) == len(system_ids())


def test_a_prior_naming_a_decommissioned_system_is_harmless() -> None:
    """Stale priors are expected (ADR-019 cost 2) and must not corrupt the sweep."""
    ordered = ordered_candidates(["legacy-crm holds everything for this tenant"])
    assert set(ordered) == set(system_ids())
