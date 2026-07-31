"""`compliance-archive` — S3 Object Lock COMPLIANCE + KMS. **Nothing here can be deleted.**

An object under COMPLIANCE-mode retention cannot be removed by anyone, including the
account root, until retention expires. There is no API call that satisfies an erasure
request against this bucket, and no IAM policy that changes that. It is the archetype
that forces the question: what does "erase" mean when deletion is unavailable?

The answer is cryptographic. Every object for a subject is encrypted client-side under a
per-subject data encryption key; the DEK is wrapped by the tenant CMK; and the wrapped
DEK exists in exactly one place — the DEK registry table, which has point-in-time
recovery disabled and is excluded from every backup path (invariant 14). `hard_delete`
deletes that one item. The ciphertext remains and is permanently unreadable.

**Why the shred is at the DEK layer and not the CMK layer.** `kms:ScheduleKeyDeletion`
enforces a minimum seven-day pending window that cannot be shortened. A crypto-shred
implemented as "destroy the KMS key" could therefore never return `APPLIED` — it would
return `PARTIAL` with a multi-week residual, and the Certificate of Erasure would be
unissuable inside a one-month statutory deadline. Deleting the wrapped DEK is immediate.
The CMK is tenant-lifetime and outlives any single subject (ADR-007).

**The legal caveat is carried, not resolved.** Whether cryptographic erasure satisfies
GDPR Art. 17 is jurisdiction-dependent and unsettled: several supervisory authorities
accept it, others treat it as pseudonymisation. This code implements the mechanism; it
does not settle the question, and the docs say so rather than asserting otherwise.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pii_erasure.contract import (
    DEK_ARTIFACT_KIND,
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

SYSTEM_ID = "compliance-archive"

#: The ciphertext objects. Locked, undeletable, and — once the DEK is gone — noise.
CIPHERTEXT_KIND = "locked-object"

#: The wrapped data key. The only thing here that *can* be destroyed, which is why it is
#: the whole erasure mechanism.
#: The planner matches on exactly this kind to fill the manifest's `dekRegistryRef`,
#: so the string lives in `contract/` and both layers read it from there (V13-15).
DEK_KIND = DEK_ARTIFACT_KIND

READABLE = "READABLE"
SHREDDED = "SHREDDED"
ABSENT = "ABSENT"


class ComplianceArchive(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.WORM

    #: Declared at plan time, not discovered at execution time: the ciphertext can never
    #: be removed, so `discover` reports PARTIAL and the approver sees the truth before
    #: approving rather than reading it in a certificate afterwards (invariant 7).
    undeletable_kinds = frozenset({CIPHERTEXT_KIND})

    def __init__(
        self,
        bucket: str,
        dek_table: str,
        *,
        s3: Any | None = None,
        dynamodb: Any | None = None,
    ) -> None:
        self._bucket = bucket
        self._dek_table = dek_table
        self._s3 = s3 or boto3.client("s3")
        self._ddb = dynamodb or boto3.client("dynamodb")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        objects = self._objects(request.subject_ref)
        dek = self._dek(request.subject_ref)

        artifacts: list[Artifact] = []
        if objects:
            artifacts.append(
                Artifact(
                    kind=CIPHERTEXT_KIND,
                    locator=self._prefix(request.subject_ref),
                    count=len(objects),
                    classification=("PII", "ARCHIVED"),
                )
            )
        if dek is not None:
            artifacts.append(
                Artifact(
                    kind=DEK_KIND,
                    locator=f"{self._dek_table}#{request.subject_ref}",
                    count=1,
                    classification=("KEY_MATERIAL",),
                )
            )

        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, (), undeletable_kinds=self.undeletable_kinds),
            artifacts=tuple(artifacts),
            evidence=discovery_evidence(
                {"bucket": self._bucket, "prefix": self._prefix(request.subject_ref)}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        """Clean means *unreadable*, which is not the same as gone.

        The distinction the conformance suite asserts: after a shred, the objects are
        still listed and the key is not. A participant that reported "nothing here"
        would be claiming a deletion that did not happen — and the difference matters to
        an auditor, who can confirm the ciphertext survived and the key did.
        """
        state = self._readability(request.subject_ref)
        objects = self._objects(request.subject_ref)
        remaining = (
            (
                Artifact(
                    kind=CIPHERTEXT_KIND,
                    locator=self._prefix(request.subject_ref),
                    count=len(objects),
                ),
            )
            if state == READABLE
            else ()
        )
        return VerifyResponse(
            system_id=self.system_id,
            clean=state in (SHREDDED, ABSENT),
            remaining=remaining,
            evidence=discovery_evidence(
                {"readability": state, "objects": len(objects), "verify": True}
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        """Revoke the read grant. Reversible: the key material is untouched."""
        if self._dek(request.subject_ref) is None:
            return MutationResponse(
                system_id=self.system_id,
                outcome=Outcome.APPLIED,
                affected=0,
                evidence=receipt_evidence({"revoked": False, "reason": "no key registered"}),
            )
        self._ddb.update_item(
            TableName=self._dek_table,
            Key={"subject_ref": {"S": request.subject_ref}},
            UpdateExpression="SET revoked = :true",
            ExpressionAttributeValues={":true": {"BOOL": True}},
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"revoked": True}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        self._ddb.update_item(
            TableName=self._dek_table,
            Key={"subject_ref": {"S": request.subject_ref}},
            UpdateExpression="SET revoked = :false",
            ExpressionAttributeValues={":false": {"BOOL": False}},
            ConditionExpression="attribute_exists(subject_ref)",
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            evidence=receipt_evidence({"revoked": False}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        """The shred. One `DeleteItem`, and no key material for this subject exists anywhere.

        Returns `APPLIED` rather than `PARTIAL`: the ciphertext that remains is, on the
        crypto-shred argument, no longer personal data. That claim is exactly what the
        legal caveat in this module's docstring is about — the mechanism is unambiguous,
        its legal sufficiency is not, and the ADR records both.
        """
        existed = self._dek(request.subject_ref) is not None
        self._ddb.delete_item(
            TableName=self._dek_table, Key={"subject_ref": {"S": request.subject_ref}}
        )
        objects = self._objects(request.subject_ref)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1 if existed else 0,
            evidence=receipt_evidence(
                {"shredded": existed, "ciphertextObjectsRetained": len(objects)}
            ),
        )

    # ── AWS detail ───────────────────────────────────────────────────────────────────

    def _prefix(self, subject_ref: str) -> str:
        return f"{subject_ref}/"

    def _objects(self, subject_ref: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix(subject_ref)):
            found.extend(page.get("Contents", []))
        return found

    def _dek(self, subject_ref: str) -> dict[str, Any] | None:
        try:
            item: dict[str, Any] | None = self._ddb.get_item(
                TableName=self._dek_table,
                Key={"subject_ref": {"S": subject_ref}},
                ConsistentRead=True,
            ).get("Item")
        except ClientError:
            raise
        return item

    def _readability(self, subject_ref: str) -> str:
        """Distinguish shredded from never-present — the trap this milestone names."""
        objects = self._objects(subject_ref)
        dek = self._dek(subject_ref)
        if not objects and dek is None:
            return ABSENT
        if dek is None:
            return SHREDDED
        return READABLE


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = ComplianceArchive(
        os.environ["ARCHIVE_BUCKET_NAME"], os.environ["DEK_REGISTRY_TABLE"]
    )
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
