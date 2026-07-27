"""Recompute the chain and compare — `make ledger` runs this (M8 wires the CLI).

Verification recomputes every digest from the entry bodies and walks the links. It
never trusts a stored digest as input to anything except the comparison itself.

Framework-free and boto3-free: it verifies `LedgerEntry` values wherever they came
from — the live table via `LedgerWriter.entries()`, or the Object Lock archive export.
"""

from __future__ import annotations

from collections.abc import Sequence

from pii_erasure.ledger.chain import GENESIS_DIGEST, ChainError, LedgerEntry, entry_digest


def verify_chain(entries: Sequence[LedgerEntry]) -> int:
    """Verify one saga's chain end to end. Returns the number of verified entries.

    Raises `ChainError` naming the first sequence number that fails — an auditor's
    first question is *where* the chain broke, not merely that it did.
    """
    prev_digest = GENESIS_DIGEST
    for position, entry in enumerate(entries):
        if entry.seq != position:
            raise ChainError(
                f"seq {entry.seq} found at position {position} — an entry was dropped, "
                "duplicated, or reordered"
            )
        if entry.prev_digest != prev_digest:
            raise ChainError(f"seq {entry.seq}: prev_digest does not match the chain tail")
        recomputed = entry_digest(
            saga_id=entry.saga_id,
            seq=entry.seq,
            event_type=entry.event_type,
            at=entry.at,
            body=entry.body,
            prev_digest=entry.prev_digest,
        )
        if recomputed != entry.digest:
            raise ChainError(f"seq {entry.seq}: stored digest does not match its body")
        prev_digest = entry.digest
    return len(entries)
