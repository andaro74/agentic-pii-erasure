"""Hash-chained audit ledger (ADR-010): DynamoDB append → Streams → S3 Object Lock.

Framework-free by invariant 0 — the ledger survived two framework migrations untouched
and must survive a third.
"""

from pii_erasure.ledger.chain import GENESIS_DIGEST, ChainError, LedgerEntry, entry_digest
from pii_erasure.ledger.verify import verify_chain
from pii_erasure.ledger.writer import LedgerWriter

__all__ = [
    "GENESIS_DIGEST",
    "ChainError",
    "LedgerEntry",
    "LedgerWriter",
    "entry_digest",
    "verify_chain",
]
