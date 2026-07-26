"""`vector-index` — Amazon S3 Vectors. **An embedding outlives its source.**

A derived store is the one people forget. Delete the profile row and the embedding
computed from it remains, still semantically searchable, still recoverable in substance by
anyone who can query nearest neighbours. An embedding of personal data *is* personal data.
Priya Raghunathan exists in the seed set precisely as an orphan: her source record is
already gone and only the vectors remain, so recall has to find a subject whose
"authoritative" systems all report absent.

**S3 Vectors has no delete-by-query.** `DeleteVectors` takes `keys` and nothing else —
verified against the service model, not assumed. There is no filter, no prefix, no
"delete where metadata.subjectRef = …". That single constraint dictates the design
([ADR-021](../../../../docs/adr/ADR-021-s3-vectors-for-cost.md)):

* Keys are **derived deterministically** from `subjectRef` — ``<subjectRef>#<ordinal>``.
  Nothing needs to be looked up to know what to delete.
* There is deliberately **no side mapping table**. A table of subject → vector keys would
  be a second source of truth that can be lost, corrupted, or restored to a stale point
  independently of the vectors it addresses — and losing it would leave the embeddings
  fully present and permanently unaddressable. Un-deletable personal data created by our
  own bookkeeping is a worse outcome than any it would prevent.
* Existence is probed with `GetVectors`, which caps at **100 keys per call** — not the 500
  that `PutVectors` and `DeleteVectors` allow (V8-2). Batching all three at 500 works in
  every small test and fails on the first subject with a real corpus.

This is why the pseudonymous handle must stay alive until last: it is the only join key,
and the architecture treats "keep the identifier until the end" as a hard requirement
rather than a tidy convention.

**Metadata is a PII surface.** Vector metadata here carries the `subjectRef` and a
non-identifying source kind — never bio text, never an email, never a name.
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

SYSTEM_ID = "vector-index"

#: Per-call ceilings from the S3 Vectors service model. Distinct on purpose — see V8-2.
_GET_BATCH = 100
_DELETE_BATCH = 500
_PUT_BATCH = 500

#: How far the deterministic key space is probed for one subject. A ceiling on the seeded
#: corpus, not a guess about the data: the generator writes within it, and a subject that
#: needed more would need this raised in both places at once, which is why it is one
#: constant rather than two conventions.
MAX_VECTORS_PER_SUBJECT = 200

#: Embedding width. Declared here rather than in the stack because a vector written at a
#: different dimension than the index expects is rejected at write time, and the two must
#: therefore agree — so `infra/` imports this constant instead of restating it. Small on
#: purpose: the archetype's lesson is that an embedding is personal data with no
#: delete-by-query, not that it is high-fidelity.
VECTOR_DIMENSION = 8

SOFT_DELETE_KEY = "asdpState"
SOFT_DELETE_VALUE = "pending-delete"


def vector_key(subject_ref: str, ordinal: int) -> str:
    """The only mapping from subject to vector keys. Deterministic, stored nowhere."""
    return f"{subject_ref}#{ordinal:04d}"


class VectorIndex(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.DERIVED_INDEX

    def __init__(self, bucket_name: str, index_name: str, *, client: Any | None = None) -> None:
        self._bucket = bucket_name
        self._index = index_name
        self._vectors = client or boto3.client("s3vectors")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        present = self._present_keys(request.subject_ref)
        artifacts: tuple[Artifact, ...] = ()
        if present:
            artifacts = (
                Artifact(
                    kind="vector",
                    locator=self._locator(request.subject_ref),
                    count=len(present),
                    classification=("PII", "DERIVED"),
                ),
            )
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, ()),
            artifacts=artifacts,
            evidence=discovery_evidence(
                {"vectorBucket": self._bucket, "index": self._index, "probed": True}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        present = self._present_keys(request.subject_ref)
        remaining = (
            (
                Artifact(
                    kind="vector", locator=self._locator(request.subject_ref), count=len(present)
                ),
            )
            if present
            else ()
        )
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=remaining,
            evidence=discovery_evidence(
                {"vectorBucket": self._bucket, "index": self._index, "verify": True}
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        """Mark, reversibly. S3 Vectors has no metadata-only update, so the vectors are
        read back and re-put with a flag — the data is unchanged, only the metadata moves.
        """
        vectors = self._fetch(request.subject_ref, with_data=True)
        self._put(self._reflagged(vectors, pending=True))
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(vectors),
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"marked": len(vectors)}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        vectors = self._fetch(request.subject_ref, with_data=True)
        self._put(self._reflagged(vectors, pending=False))
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(vectors),
            evidence=receipt_evidence({"unmarked": len(vectors)}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        keys = self._present_keys(request.subject_ref)
        for start in range(0, len(keys), _DELETE_BATCH):
            self._vectors.delete_vectors(
                vectorBucketName=self._bucket,
                indexName=self._index,
                keys=keys[start : start + _DELETE_BATCH],
            )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=len(keys),
            evidence=receipt_evidence({"deletedVectors": len(keys)}),
        )

    # ── S3 Vectors detail ────────────────────────────────────────────────────────────

    def _locator(self, subject_ref: str) -> str:
        return f"s3vectors://{self._bucket}/{self._index}/{subject_ref}#"

    def _candidate_keys(self, subject_ref: str) -> list[str]:
        return [vector_key(subject_ref, n) for n in range(MAX_VECTORS_PER_SUBJECT)]

    def _fetch(self, subject_ref: str, *, with_data: bool) -> list[dict[str, Any]]:
        """Probe the derived key space. Absent keys are simply not returned."""
        found: list[dict[str, Any]] = []
        candidates = self._candidate_keys(subject_ref)
        for start in range(0, len(candidates), _GET_BATCH):
            response = self._vectors.get_vectors(
                vectorBucketName=self._bucket,
                indexName=self._index,
                keys=candidates[start : start + _GET_BATCH],
                returnData=with_data,
                returnMetadata=True,
            )
            found.extend(response.get("vectors", []))
        return found

    def _present_keys(self, subject_ref: str) -> list[str]:
        return sorted(str(vector["key"]) for vector in self._fetch(subject_ref, with_data=False))

    def _reflagged(self, vectors: list[dict[str, Any]], *, pending: bool) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        for vector in vectors:
            metadata = dict(vector.get("metadata") or {})
            if pending:
                metadata[SOFT_DELETE_KEY] = SOFT_DELETE_VALUE
            else:
                metadata.pop(SOFT_DELETE_KEY, None)
            updated.append({"key": vector["key"], "data": vector["data"], "metadata": metadata})
        return updated

    def _put(self, vectors: list[dict[str, Any]]) -> None:
        for start in range(0, len(vectors), _PUT_BATCH):
            self._vectors.put_vectors(
                vectorBucketName=self._bucket,
                indexName=self._index,
                vectors=vectors[start : start + _PUT_BATCH],
            )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = VectorIndex(os.environ["VECTOR_BUCKET_NAME"], os.environ["VECTOR_INDEX_NAME"])
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
