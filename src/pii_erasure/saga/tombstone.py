"""The tombstone registry (ARCHITECTURE §5.3) — the anti-resurrection control.

A DynamoDB table keyed by the *hash* of the subject handle. Entries outlive the subject
data permanently: a tombstoned subject cannot be re-created, and every participant write
path is expected to consult this table (the participants' side lands with the write-path
guards; the saga's side — recording the tombstone at phase-3 completion and refusing to
run for an already-tombstoned subject — is here).

The key is a hash, not the handle itself, so the registry that outlives everything else
holds nothing that links back to the subject (invariant 5 applied to our own tables).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import boto3


def subject_hash(subject_ref: str) -> str:
    """The permanent registry key. Deterministic so every write path derives the same."""
    return "sha256:" + hashlib.sha256(subject_ref.encode("utf-8")).hexdigest()


class TombstoneRegistry:
    """Append-only registry over the foundation `tombstones` table."""

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table = table_name
        self._ddb = client or boto3.client("dynamodb")

    def is_tombstoned(self, subject_ref: str) -> bool:
        response = self._ddb.get_item(
            TableName=self._table,
            Key={"subject_hash": {"S": subject_hash(subject_ref)}},
            ConsistentRead=True,
        )
        return "Item" in response

    def record(self, subject_ref: str, *, saga_id: str) -> None:
        """Record erasure. Idempotent per subject — a re-executed node re-records.

        The conditional write tolerates only *this saga's* prior write: two different
        sagas tombstoning the same subject would mean the intake guard failed, and that
        is worth failing loudly over rather than absorbing.
        """
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item={
                    "subject_hash": {"S": subject_hash(subject_ref)},
                    "saga_id": {"S": saga_id},
                    "erased_at": {
                        "S": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    },
                },
                ConditionExpression=("attribute_not_exists(subject_hash) OR saga_id = :saga_id"),
                ExpressionAttributeValues={":saga_id": {"S": saga_id}},
            )
        except self._ddb.exceptions.ConditionalCheckFailedException:
            raise TombstoneConflictError(
                "subject is already tombstoned by a different saga — the intake guard "
                "should have refused this run before anything mutated"
            ) from None


class TombstoneConflictError(RuntimeError):
    """Two sagas erased the same subject. A control upstream failed."""
