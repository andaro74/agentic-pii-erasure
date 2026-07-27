"""Append-only ledger writes to DynamoDB.

Appends are **conditional on the sequence slot being empty**, so two concurrent writers
cannot both claim seq N — one loses the race, re-reads the tail, and retries at N+1.
That property, not goodwill, is what "append-only" means here. Tamper evidence
downstream is Streams → Firehose → S3 Object Lock (foundation stack, ADR-010); the
chain digests are what make an edit *visible*, the archive is what makes it *futile*.

Bodies must already be pseudonymous: `subject_ref`, digests, counts. Never raw PII
(invariant 5) — the ledger is retained for seven years, which is seven years of
liability for anything that leaks into it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import boto3

from pii_erasure.ledger.chain import GENESIS_DIGEST, LedgerEntry, make_entry
from pii_erasure.observability.redact import scrub_mapping

_MAX_APPEND_RACES = 5


class LedgerAppendError(RuntimeError):
    """The append could not claim a sequence slot after bounded retries."""


class LedgerWriter:
    """Per-saga append and read over the foundation ledger table."""

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._ddb = client or boto3.client("dynamodb")

    def append(self, *, saga_id: str, event_type: str, body: dict[str, Any]) -> LedgerEntry:
        """Append one entry, chained to the current tail. Safe under concurrency.

        The body is scrubbed on the way in (invariant 5): callers construct
        pseudonymous bodies, and the scrubber is the backstop for the seven-year
        retention window, not the primary control.
        """
        body = scrub_mapping(body)
        for _ in range(_MAX_APPEND_RACES):
            tail = self._tail(saga_id)
            seq = (tail.seq + 1) if tail else 0
            prev_digest = tail.digest if tail else GENESIS_DIGEST
            entry = make_entry(
                saga_id=saga_id,
                seq=seq,
                event_type=event_type,
                at=_now(),
                body=body,
                prev_digest=prev_digest,
            )
            try:
                self._ddb.put_item(
                    TableName=self._table,
                    Item=_to_item(entry),
                    ConditionExpression=(
                        "attribute_not_exists(saga_id) AND attribute_not_exists(seq)"
                    ),
                )
            except self._ddb.exceptions.ConditionalCheckFailedException:
                continue  # lost the race for this slot — re-read the tail and retry
            return entry
        raise LedgerAppendError(
            f"could not append to saga {saga_id} after {_MAX_APPEND_RACES} attempts"
        )

    def entries(self, saga_id: str) -> list[LedgerEntry]:
        """Every entry for one saga, in sequence order, consistently read."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "saga_id = :sid",
            "ExpressionAttributeValues": {":sid": {"S": saga_id}},
            "ConsistentRead": True,
        }
        while True:
            page = self._ddb.query(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return [_from_item(item) for item in items]

    def _tail(self, saga_id: str) -> LedgerEntry | None:
        page = self._ddb.query(
            TableName=self._table,
            KeyConditionExpression="saga_id = :sid",
            ExpressionAttributeValues={":sid": {"S": saga_id}},
            ScanIndexForward=False,
            Limit=1,
            ConsistentRead=True,
        )
        items = page.get("Items", [])
        return _from_item(items[0]) if items else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_item(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "saga_id": {"S": entry.saga_id},
        "seq": {"N": str(entry.seq)},
        "event_type": {"S": entry.event_type},
        "at": {"S": entry.at},
        "body": {"S": json.dumps(entry.body, sort_keys=True, ensure_ascii=False)},
        "prev_digest": {"S": entry.prev_digest},
        "digest": {"S": entry.digest},
    }


def _from_item(item: dict[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        saga_id=item["saga_id"]["S"],
        seq=int(item["seq"]["N"]),
        event_type=item["event_type"]["S"],
        at=item["at"]["S"],
        body=json.loads(item["body"]["S"]),
        prev_digest=item["prev_digest"]["S"],
        digest=item["digest"]["S"],
    )
