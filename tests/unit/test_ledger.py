"""The hash chain and the append-only writer.

Chain tests are pure. Writer tests use an in-memory fake DynamoDB client that honours
the ONE property the writer depends on — the conditional put on an empty (saga_id,
seq) slot. A fake can testify about the writer's *logic*; the real table's behaviour
is the deployed gate's business.
"""

from __future__ import annotations

from typing import Any

import pytest

from pii_erasure.ledger import ChainError, LedgerWriter, verify_chain
from pii_erasure.ledger.chain import GENESIS_DIGEST, make_entry
from pii_erasure.ledger.writer import LedgerAppendError

# ─── chain ────────────────────────────────────────────────────────────────────────────


def _chain_of(n: int) -> list[Any]:
    entries = []
    prev = GENESIS_DIGEST
    for seq in range(n):
        entry = make_entry(
            saga_id="saga_1",
            seq=seq,
            event_type=f"EVENT_{seq}",
            at="2026-07-26T12:00:00Z",
            body={"n": seq},
            prev_digest=prev,
        )
        entries.append(entry)
        prev = entry.digest
    return entries


def test_a_well_formed_chain_verifies() -> None:
    assert verify_chain(_chain_of(5)) == 5
    assert verify_chain([]) == 0


def test_an_edited_body_breaks_the_chain_at_that_entry() -> None:
    entries = _chain_of(5)
    from dataclasses import replace

    entries[2] = replace(entries[2], body={"n": 999})
    with pytest.raises(ChainError, match="seq 2"):
        verify_chain(entries)


def test_a_dropped_entry_is_detected() -> None:
    entries = _chain_of(5)
    del entries[3]
    with pytest.raises(ChainError):
        verify_chain(entries)


def test_a_reordered_chain_is_detected() -> None:
    entries = _chain_of(4)
    entries[1], entries[2] = entries[2], entries[1]
    with pytest.raises(ChainError):
        verify_chain(entries)


def test_a_forged_digest_is_detected() -> None:
    entries = _chain_of(3)
    from dataclasses import replace

    entries[1] = replace(entries[1], digest="sha256:" + "f" * 64)
    with pytest.raises(ChainError):
        verify_chain(entries)


def test_floats_are_refused_in_bodies() -> None:
    with pytest.raises(ChainError, match="float"):
        make_entry(
            saga_id="s",
            seq=0,
            event_type="E",
            at="2026-07-26T12:00:00Z",
            body={"ratio": 0.5},
            prev_digest=GENESIS_DIGEST,
        )


# ─── writer over a conditional-put-honouring fake ─────────────────────────────────────


class _CondError(Exception):
    pass


class _FakeDdb:
    """Honours attribute_not_exists on the composite key; everything else is a dict."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, int], dict[str, Any]] = {}
        self.fail_next_puts = 0

        class _Exceptions:
            ConditionalCheckFailedException = _CondError

        self.exceptions = _Exceptions()

    # boto3's CamelCase calling convention, mirrored so the writer's real call works.
    def put_item(
        self,
        *,
        TableName: str,  # noqa: N803
        Item: dict[str, Any],  # noqa: N803
        ConditionExpression: str,  # noqa: N803
    ) -> None:
        key = (Item["saga_id"]["S"], int(Item["seq"]["N"]))
        if key in self.items or self.fail_next_puts > 0:
            self.fail_next_puts = max(0, self.fail_next_puts - 1)
            raise _CondError()
        self.items[key] = Item

    def query(self, **kwargs: Any) -> dict[str, Any]:
        saga_id = kwargs["ExpressionAttributeValues"][":sid"]["S"]
        rows = sorted(
            (item for (sid, _), item in self.items.items() if sid == saga_id),
            key=lambda item: int(item["seq"]["N"]),
            reverse=not kwargs.get("ScanIndexForward", True),
        )
        limit = kwargs.get("Limit")
        return {"Items": rows[:limit] if limit else rows}


def test_writer_appends_a_verifying_chain() -> None:
    ddb = _FakeDdb()
    writer = LedgerWriter("ledger", client=ddb)
    writer.append(saga_id="saga_9", event_type="A", body={"x": 1})
    writer.append(saga_id="saga_9", event_type="B", body={"x": 2})
    writer.append(saga_id="saga_9", event_type="C", body={"x": 3})

    entries = writer.entries("saga_9")
    assert [e.event_type for e in entries] == ["A", "B", "C"]
    assert verify_chain(entries) == 3


def test_writer_retries_past_a_lost_race() -> None:
    ddb = _FakeDdb()
    writer = LedgerWriter("ledger", client=ddb)
    writer.append(saga_id="saga_9", event_type="A", body={})
    ddb.fail_next_puts = 2  # two losses, then the slot is free
    writer.append(saga_id="saga_9", event_type="B", body={})
    assert verify_chain(writer.entries("saga_9")) == 2


def test_writer_gives_up_loudly_after_bounded_races() -> None:
    ddb = _FakeDdb()
    ddb.fail_next_puts = 99
    writer = LedgerWriter("ledger", client=ddb)
    with pytest.raises(LedgerAppendError):
        writer.append(saga_id="saga_9", event_type="A", body={})


def test_writer_scrubs_pii_shaped_values_on_the_way_in() -> None:
    """Invariant 5's backstop: seven years of retention, zero tolerance."""
    ddb = _FakeDdb()
    writer = LedgerWriter("ledger", client=ddb)
    entry = writer.append(
        saga_id="saga_9",
        event_type="A",
        body={"note": "contact yuki.tanaka@example.com", "subjectRef": "sub_abc123"},
    )
    assert "yuki.tanaka" not in str(entry.body)
    assert entry.body["subjectRef"] == "sub_abc123", "pseudonymous handles must survive"
