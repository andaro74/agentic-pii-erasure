"""The applied-key log (ARCHITECTURE §4.3). One DynamoDB table, partitioned per system.

Phase 3 has no compensation (invariant 6), so a duplicate `hard_delete` cannot be undone
— only prevented. And duplicates are not exotic here: Lambda retries, EventBridge
Scheduler's at-least-once delivery, checkpoint resume after a crash, and operator re-runs
all replay the same call.

**Claim before applying, not after.** Recording the key after the work would leave a
window where a crash between "deleted" and "recorded" replays as a fresh call. So a
mutation claims its key with a conditional put, does the work, then stores its response.
A replay that finds a *completed* claim returns the original response with the outcome
rewritten to `ALREADY_APPLIED`; one that finds an *in-flight* claim raises, because
answering "already applied" for work that may still be running would be a lie the caller
cannot detect.
"""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

#: Idempotency records outlive the saga that created them by a wide margin — the
#: contract requires ≥ max saga lifetime, and a saga can sit at the approval gate for
#: 30 days before a 30-day grace window even starts.
RETENTION_SECONDS = 400 * 24 * 60 * 60

STATUS_IN_FLIGHT = "IN_FLIGHT"
STATUS_COMPLETED = "COMPLETED"


class ReplayInFlightError(RuntimeError):
    """A duplicate arrived while the original call is still running.

    Deliberately an error rather than an `ALREADY_APPLIED`: the first call may still
    fail, and a caller told "already applied" would stop retrying something that never
    happened.
    """


class IdempotencyLog:
    """Thin wrapper over the DynamoDB table. No caching — a cache here is a correctness
    bug waiting for a cold start."""

    def __init__(self, table_name: str, *, client: Any | None = None) -> None:
        self._table_name = table_name
        self._client = client or boto3.client("dynamodb")

    def claim(self, *, system_id: str, key: str) -> dict[str, Any] | None:
        """Claim `key`. Returns None when the claim is ours, or the prior record on replay.

        The conditional put is the whole mechanism: two concurrent invocations race, and
        exactly one wins.
        """
        now = int(time.time())
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    "system_id": {"S": system_id},
                    "idempotency_key": {"S": key},
                    "status": {"S": STATUS_IN_FLIGHT},
                    "claimed_at": {"N": str(now)},
                    "expires_at": {"N": str(now + RETENTION_SECONDS)},
                },
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
            return None
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return self._read(system_id=system_id, key=key)

    def complete(self, *, system_id: str, key: str, response: dict[str, Any]) -> None:
        """Store the response that this key produced, so a replay can return it verbatim."""
        self._client.update_item(
            TableName=self._table_name,
            Key={"system_id": {"S": system_id}, "idempotency_key": {"S": key}},
            UpdateExpression="SET #s = :done, #r = :response",
            ExpressionAttributeNames={"#s": "status", "#r": "response"},
            ExpressionAttributeValues={
                ":done": {"S": STATUS_COMPLETED},
                ":response": {"S": json.dumps(response, sort_keys=True)},
            },
        )

    def release(self, *, system_id: str, key: str) -> None:
        """Drop a claim whose work failed, so a retry is a fresh attempt rather than a
        permanent `ALREADY_APPLIED` for something that never happened."""
        self._client.delete_item(
            TableName=self._table_name,
            Key={"system_id": {"S": system_id}, "idempotency_key": {"S": key}},
        )

    def _read(self, *, system_id: str, key: str) -> dict[str, Any]:
        item = self._client.get_item(
            TableName=self._table_name,
            Key={"system_id": {"S": system_id}, "idempotency_key": {"S": key}},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            # The claim existed a moment ago and is gone now — a concurrent release.
            # Treat it as in-flight: the safe answer is "retry", never "already done".
            raise ReplayInFlightError(f"{system_id}: claim for {key} vanished mid-flight")
        if item.get("status", {}).get("S") != STATUS_COMPLETED:
            raise ReplayInFlightError(f"{system_id}: {key} is still in flight")
        stored: dict[str, Any] = json.loads(item["response"]["S"])
        return stored
