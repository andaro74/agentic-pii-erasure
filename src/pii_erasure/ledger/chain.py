"""The hash chain. Each entry's digest covers its body *and* its predecessor's digest.

Tamper evidence is the point: editing entry N changes its digest, which breaks entry
N+1's `prev_digest` link, which `verify_chain` catches. The chain lives per saga —
`saga_id` is the partition key and `seq` the monotonic sort key — so verification is a
single-partition read.

**Why this does NOT use `contract.canonical`.** That encoder exists for approval
digests, and its rule 5 *rejects* volatile keys — timestamps, trace IDs, `digest`
itself — because nothing volatile may enter an approved body (invariant 4). A ledger
entry is the opposite case: *when* something happened and *what digest it carried* are
precisely the facts being chained. So the ledger uses plain sorted-key compact JSON —
deterministic (sorted keys, no floats permitted in bodies, UTF-8), but with none of the
approval-digest restrictions. The divergence is deliberate; collapsing the two encoders
would force one domain's rules onto the other's data.

Pure functions and a frozen dataclass. No boto3, no framework (invariant 0) — this file
must be liftable next to `contract/`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: The `prev_digest` of the first entry in every saga's chain. A constant, not an empty
#: string, so an accidentally-empty field cannot masquerade as genesis.
GENESIS_DIGEST = "sha256:" + "0" * 64


class ChainError(ValueError):
    """The chain does not verify — an entry was altered, dropped, or reordered."""


@dataclass(frozen=True)
class LedgerEntry:
    """One appended fact. Bodies carry `subject_ref` at most — never raw PII."""

    saga_id: str
    seq: int
    event_type: str
    at: str  # ISO-8601 UTC. Digested: *when* something was recorded is part of the fact.
    body: dict[str, Any]
    prev_digest: str
    digest: str


def _stable_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes for hashing. Floats are refused — counts are integers here
    exactly as they are in the contract, and float formatting is the classic source of
    silent digest drift."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if any(isinstance(value, float) for value in _walk(payload)):
        raise ChainError("floats are not hashable stably — use integers in ledger bodies")
    return text.encode("utf-8")


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    else:
        yield value


def entry_digest(
    *,
    saga_id: str,
    seq: int,
    event_type: str,
    at: str,
    body: dict[str, Any],
    prev_digest: str,
) -> str:
    """The digest over everything an entry asserts, including its place in the chain."""
    payload = {
        "sagaId": saga_id,
        "seq": seq,
        "eventType": event_type,
        "at": at,
        "body": body,
        "prevDigest": prev_digest,
    }
    return "sha256:" + hashlib.sha256(_stable_bytes(payload)).hexdigest()


def make_entry(
    *,
    saga_id: str,
    seq: int,
    event_type: str,
    at: str,
    body: dict[str, Any],
    prev_digest: str,
) -> LedgerEntry:
    return LedgerEntry(
        saga_id=saga_id,
        seq=seq,
        event_type=event_type,
        at=at,
        body=body,
        prev_digest=prev_digest,
        digest=entry_digest(
            saga_id=saga_id,
            seq=seq,
            event_type=event_type,
            at=at,
            body=body,
            prev_digest=prev_digest,
        ),
    )
