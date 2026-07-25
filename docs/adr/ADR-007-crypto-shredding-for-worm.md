# ADR-007: Crypto-shred for WORM participants

- **Status:** Accepted — **legal sign-off is a release gate**
- **Anchors invariants:** CLAUDE.md #7 (residual honesty)
- **Baseline:** architecture v0.1

## Context

An S3 bucket in Object Lock COMPLIANCE mode cannot be deleted from by anyone —
including the root account — until retention expires. Append-only event logs and
columnar Parquet files have the same property: **no API call satisfies a row-level
erasure request.** Waiting for retention to expire is not erasure, and can put the
system past a statutory deadline.

Deletion here has to be redefined as *irreversible loss of readability* rather than
removal of bytes.

## Decision

Encrypt each subject's objects under a **per-subject data encryption key (DEK)**,
and implement `hard_delete` for WORM participants as **destruction of that key**.
After shred, the ciphertext remains on immutable media but is unrecoverable; a
decryption *failure* is distinguishable from a *not-found*, which is what `verify`
asserts.

The DEK registry is the crown jewel and the hazard: **it is excluded from every
backup and copy path** (in production, KMS envelope keys with point-in-time recovery
*disabled*). A backed-up key un-shreds the subject.

`aegis-archive` is the worked example. It reports its mechanism honestly and, where
work remains, returns `PARTIAL` with a populated `residual` (invariant #7).

## Consequences

- **Positive.** Gives an erasure mechanism for stores that have none, at the very end
  of phase 3 (crypto-shred is the only genuinely unrecoverable step, so it goes last).
- **Open legal question.** Crypto-shredding's sufficiency as "erasure" under GDPR
  Art. 17 is **jurisdiction-dependent and unsettled** — some supervisory authorities
  accept it, others treat it as pseudonymisation. This ADR records a *position*, not
  a resolution, and makes counsel sign-off a release gate. The architecture doc's
  caveat here is deliberate and must not be "improved" into a confident claim.

### Enforcement

- The DEK registry's exclusion from any backup/copy path is asserted by test (ROADMAP M2 trap).

## Alternatives considered

- **Wait for retention expiry.** Rejected: not erasure; misses statutory deadlines.

## References

- ARCHITECTURE.md §4.2 (archetype 5, legal caveat), §5.2 (shred last), §15 (ADR-007)
- README "Known limits" · [ADR-002](ADR-002-three-phase-split-recovery.md)
