"""`profile-store` — Amazon DynamoDB. **A GSI is not the table, and a TTL is not a delete.**

Two failure modes live here, and both produce a *recall* failure rather than a crash —
the class ADR-008 exists to prevent, because nothing goes red at the time.

**Global secondary indexes are eventually consistent.** A GSI cannot be read with
`ConsistentRead`; DynamoDB rejects the parameter outright. So an item deleted from the
base table can still be returned by a GSI query for some period afterwards, and an item
just written may be missing from one. Every read here therefore goes to the **base table
with `ConsistentRead=True`**: `discover` must not miss a freshly written item, and
`verify` must not be reassured by a stale index. The GSI exists in the stack because a
real profile store has one, and the point is to demonstrate not using it for this.

**A TTL is not a deletion mechanism.** `hard_delete` does not set `expiresAt` and return
`APPLIED`. DynamoDB deletes expired items "typically within a few days" — it is a
best-effort background sweep with no SLA, and an item whose TTL has passed is still
returned by reads until the sweep reaches it. Setting a TTL and reporting the subject
erased would be a lie with a plausible-looking mechanism behind it, which is the worst
kind. `hard_delete` issues real `DeleteItem` calls and counts them.

Yuki Abramson's seeded `bio` contains a prompt-injection payload. This participant reads
it and returns it as ordinary content, because that is what a profile store does. The
defence is not here — it is that the discovery agent holding this text has no mutating
tool in its surface (invariant 1). A participant that sanitised the payload would hide
the very thing the eval needs to observe.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from pii_erasure.contract import (
    Archetype,
    Artifact,
    DiscoverRequest,
    DiscoverResponse,
    HardDeleteRequest,
    MutationResponse,
    Outcome,
    RestoreRequest,
    SoftDeleteRequest,
    VerifyRequest,
    VerifyResponse,
)
from pii_erasure.participants._base import (
    IdempotencyLog,
    Participant,
    deletability,
    discovery_evidence,
    dispatch,
    receipt_evidence,
)

SYSTEM_ID = "profile-store"

PARTITION_KEY = "subject_ref"
SORT_KEY = "item_id"

#: Marks an item pending deletion. An attribute, not a TTL — see the module docstring.
SOFT_DELETE_ATTR = "asdp_state"
SOFT_DELETE_VALUE = "pending-delete"

#: `BatchWriteItem` accepts at most 25 requests. A hard limit, not a tuning knob.
_WRITE_BATCH = 25


class ProfileStore(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.OPERATIONAL_NOSQL

    def __init__(self, table_name: str, *, resource: Any | None = None) -> None:
        self._table_name = table_name
        ddb = resource or boto3.resource("dynamodb")
        self._table = ddb.Table(table_name)

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        items = self._items(request.subject_ref)
        artifacts: tuple[Artifact, ...] = ()
        if items:
            artifacts = (
                Artifact(
                    kind="item",
                    locator=self._locator(request.subject_ref),
                    count=len(items),
                    classification=("PII", "PROFILE"),
                ),
            )
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, ()),
            artifacts=artifacts,
            evidence=discovery_evidence(
                {"table": self._table_name, "partition": request.subject_ref}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        items = self._items(request.subject_ref)
        remaining = (
            (Artifact(kind="item", locator=self._locator(request.subject_ref), count=len(items)),)
            if items
            else ()
        )
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=remaining,
            evidence=discovery_evidence(
                {"table": self._table_name, "partition": request.subject_ref, "verify": True}
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        items = self._items(request.subject_ref)
        for item in items:
            self._table.update_item(
                Key={PARTITION_KEY: item[PARTITION_KEY], SORT_KEY: item[SORT_KEY]},
                UpdateExpression="SET #state = :pending",
                ExpressionAttributeNames={"#state": SOFT_DELETE_ATTR},
                ExpressionAttributeValues={":pending": SOFT_DELETE_VALUE},
            )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(items),
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"marked": len(items)}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        items = self._items(request.subject_ref)
        for item in items:
            self._table.update_item(
                Key={PARTITION_KEY: item[PARTITION_KEY], SORT_KEY: item[SORT_KEY]},
                UpdateExpression="REMOVE #state",
                ExpressionAttributeNames={"#state": SOFT_DELETE_ATTR},
            )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(items),
            evidence=receipt_evidence({"unmarked": len(items)}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        items = self._items(request.subject_ref)
        deleted = 0
        for start in range(0, len(items), _WRITE_BATCH):
            batch = items[start : start + _WRITE_BATCH]
            with self._table.batch_writer() as writer:
                for item in batch:
                    writer.delete_item(
                        Key={PARTITION_KEY: item[PARTITION_KEY], SORT_KEY: item[SORT_KEY]}
                    )
            deleted += len(batch)

        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=deleted,
            evidence=receipt_evidence({"deletedItems": deleted, "viaTtl": False}),
        )

    # ── DynamoDB detail ──────────────────────────────────────────────────────────────

    def _locator(self, subject_ref: str) -> str:
        return f"dynamodb://{self._table_name}/{subject_ref}"

    def _items(self, subject_ref: str) -> list[dict[str, Any]]:
        """Every item in the subject's partition, strongly consistent and fully paginated.

        `ConsistentRead=True` on the **base table**: the GSI cannot offer it, and reading
        the index here is how a deleted subject keeps appearing to exist.
        """
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key(PARTITION_KEY).eq(subject_ref),
            "ConsistentRead": True,
        }
        while True:
            page = self._table.query(**kwargs)
            items.extend(page.get("Items", []))
            token = page.get("LastEvaluatedKey")
            if not token:
                return items
            kwargs["ExclusiveStartKey"] = token


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = ProfileStore(os.environ["PROFILE_TABLE"])
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
