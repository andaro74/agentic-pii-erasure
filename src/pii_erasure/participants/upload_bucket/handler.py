"""`upload-bucket` — S3 with versioning on. **A delete marker is not a deletion.**

This is the archetype that catches teams who already believed they had erasure. Calling
`DeleteObject` on a versioned bucket does not remove anything: it writes a *delete
marker* as the new current version, the object stops appearing in `ListObjectsV2`, every
previous version is still there, and a one-line API call brings it all back. Erasure that
looks complete in the console and is trivially reversible is worse than no erasure,
because it is reported as done.

So this participant deliberately never uses `DeleteObject`:

* `discover` lists **object versions and delete markers**, and reports delete markers as
  their own artifact kind. If a previous "deletion" left markers behind, the operator
  sees them named.
* `soft_delete` tags — it does not delete. Reversible by construction.
* `hard_delete` enumerates every version *and every delete marker* and removes them by
  version id, which is the only call sequence that actually destroys the bytes.
* `verify` is clean only when both lists come back empty.

Objects are laid out under ``<subjectRef>/`` so the subject's data is a prefix, and the
pseudonymous handle is the only identifier that ever reaches S3 (invariant 5).
"""

from __future__ import annotations

import os
from typing import Any

import boto3

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

SYSTEM_ID = "upload-bucket"

#: The tag that marks pending deletion. A tag rather than a lifecycle rule, because a
#: lifecycle rule is asynchronous and unobservable at the moment of the call — a soft
#: delete has to be true the instant it returns.
SOFT_DELETE_TAG = "asdp-state"
SOFT_DELETE_VALUE = "pending-delete"

#: `DeleteObjects` accepts at most 1000 keys per call. Not a tuning parameter — exceed it
#: and the call fails outright.
_DELETE_BATCH = 1000


class UploadBucket(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.DELETABLE_BLOB

    def __init__(self, bucket: str, *, client: Any | None = None) -> None:
        self._bucket = bucket
        self._s3 = client or boto3.client("s3")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        versions, markers = self._enumerate(request.subject_ref)
        artifacts: list[Artifact] = []
        if versions:
            artifacts.append(
                Artifact(
                    kind="object",
                    locator=self._prefix(request.subject_ref),
                    count=len(versions),
                    classification=("PII",),
                )
            )
        if markers:
            # Named separately and on purpose: a delete marker is the evidence that
            # somebody already believed they had deleted this.
            artifacts.append(
                Artifact(
                    kind="delete-marker",
                    locator=self._prefix(request.subject_ref),
                    count=len(markers),
                    classification=("PII",),
                )
            )

        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, ()),
            artifacts=tuple(artifacts),
            evidence=discovery_evidence(
                {"bucket": self._bucket, "prefix": self._prefix(request.subject_ref)}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        versions, markers = self._enumerate(request.subject_ref)
        remaining: list[Artifact] = []
        if versions:
            remaining.append(
                Artifact(
                    kind="object",
                    locator=self._prefix(request.subject_ref),
                    count=len(versions),
                )
            )
        if markers:
            remaining.append(
                Artifact(
                    kind="delete-marker",
                    locator=self._prefix(request.subject_ref),
                    count=len(markers),
                )
            )
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=tuple(remaining),
            evidence=discovery_evidence(
                {
                    "bucket": self._bucket,
                    "prefix": self._prefix(request.subject_ref),
                    "verify": True,
                }
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        keys = self._current_keys(request.subject_ref)
        for key in keys:
            self._s3.put_object_tagging(
                Bucket=self._bucket,
                Key=key,
                Tagging={"TagSet": [{"Key": SOFT_DELETE_TAG, "Value": SOFT_DELETE_VALUE}]},
            )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(keys),
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"tagged": sorted(keys)}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        keys = self._current_keys(request.subject_ref)
        for key in keys:
            self._s3.delete_object_tagging(Bucket=self._bucket, Key=key)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(keys),
            evidence=receipt_evidence({"untagged": sorted(keys)}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        versions, markers = self._enumerate(request.subject_ref)
        targets = [
            {"Key": entry["Key"], "VersionId": entry["VersionId"]} for entry in versions + markers
        ]
        for batch in (
            targets[index : index + _DELETE_BATCH]
            for index in range(0, len(targets), _DELETE_BATCH)
        ):
            self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": batch, "Quiet": True})

        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(targets),
            evidence=receipt_evidence(
                {"versions": len(versions), "deleteMarkers": len(markers)},
            ),
        )

    # ── S3 detail ────────────────────────────────────────────────────────────────────

    def _prefix(self, subject_ref: str) -> str:
        return f"{subject_ref}/"

    def _enumerate(self, subject_ref: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Every version and every delete marker under the subject's prefix.

        Paginated explicitly: a subject with more than a page of uploads whose later
        pages went unread is a recall failure that no test with three fixtures would
        ever catch.
        """
        versions: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        paginator = self._s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix(subject_ref)):
            versions.extend(page.get("Versions", []))
            markers.extend(page.get("DeleteMarkers", []))
        return versions, markers

    def _current_keys(self, subject_ref: str) -> list[str]:
        versions, _ = self._enumerate(subject_ref)
        return sorted({entry["Key"] for entry in versions if entry.get("IsLatest")})


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Gateway entry point. Configuration comes from the stack, never from the caller."""
    participant = UploadBucket(os.environ["UPLOAD_BUCKET_NAME"])
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
