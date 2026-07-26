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

`compliance-archive` (S3 Object Lock COMPLIANCE + KMS) is the worked example. It reports
its mechanism honestly and, where work remains, returns `PARTIAL` with a populated
`residual` (invariant #7).

> **Mechanism refined by [ADR-017](ADR-017-real-aws-participants.md): shred the DEK, not
> the CMK.** Building against real KMS surfaced a constraint no simulation would have:
> `kms:ScheduleKeyDeletion` enforces a **minimum 7-day pending window** that cannot be
> shortened. Destroying a KMS key as the shred would mean `hard_delete` could never return
> `APPLIED` — only `PARTIAL` with a 7-to-30-day residual — making the Certificate of Erasure
> unissuable inside a one-month statutory deadline. So the per-subject DEK is *wrapped* by a
> tenant CMK and the wrapped DEK is stored in a DynamoDB registry with PITR disabled; the
> shred is a registry item delete, which is immediate and irreversible. The decision above is
> unchanged; the layer it operates on moved down one.

## Consequences

- **Positive.** Gives an erasure mechanism for stores that have none, at the very end
  of phase 3 (crypto-shred is the only genuinely unrecoverable step, so it goes last).
- **Open legal question.** Crypto-shredding's sufficiency as "erasure" under GDPR
  Art. 17 is **jurisdiction-dependent and unsettled** — some supervisory authorities
  accept it, others treat it as pseudonymisation. This ADR records a *position*, not
  a resolution, and makes counsel sign-off a release gate. The architecture doc's
  caveat here is deliberate and must not be "improved" into a confident claim.

### Enforcement

- The DEK registry's exclusion from any backup/copy path is asserted by test *and* by a
  `cdk synth` assertion that the table has `pointInTimeRecovery` disabled and carries no
  AWS Backup selection (ROADMAP M2 trap). A restore of that table un-shreds every subject
  deleted since the restore point — threat T9.
- A CloudWatch alarm fires on any read of the DEK registry by a principal other than the
  `compliance-archive` participant role.

## Alternatives considered

- **Wait for retention expiry.** Rejected: not erasure; misses statutory deadlines.
- **Destroy the KMS CMK per subject.** Rejected on the 7-day `ScheduleKeyDeletion` floor
  (see the refinement note above), and because a per-subject CMK is an unbounded key
  population against a hard account quota.

## References

- ARCHITECTURE.md §4.2 (archetype 5, legal caveat), §5.2 (shred last), §15 (ADR-007)
- README "Known limits" · [ADR-002](ADR-002-three-phase-split-recovery.md)
