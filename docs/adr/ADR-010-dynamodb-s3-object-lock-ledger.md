# ADR-010: DynamoDB + S3 Object Lock for the audit ledger

- **Status:** Accepted
- **Anchors invariants:** CLAUDE.md #5 (never log raw PII)
- **Baseline:** architecture v0.1

## Context

The audit ledger must be **append-only, tamper-evident, and outlive the platform
itself** (principle P8). When a regulator asks what happened to a subject, the
ledger — signed manifest, every tool call, every policy verdict, every approver —
is the answer, and it must survive independent of the application's own IAM.

The instinct is to reach for Amazon QLDB (a purpose-built ledger database). **QLDB
is deprecated** and must not be used for new work.

## Decision

Build tamper-evidence from durable primitives instead:

- **Hash-chained entries** — each ledger entry carries its predecessor's digest, so
  any tampering breaks the chain (`ledger/chain.py`; `make ledger` recomputes and
  verifies). Locally this is a JSONL file; in production, DynamoDB.
- **Immutable export** — DynamoDB Streams → Firehose → **S3 Object Lock in
  COMPLIANCE mode**, which is auditable and durable beyond the platform's lifetime,
  and which no IAM principal (including root) can alter before retention expires.

Retention: ledger 7 years; the locked S3 export 7 years, immutable.

**No raw PII ever enters the ledger** — entries carry the pseudonymous `subject_ref`
only, scrubbed via `observability/redact.py` (invariant #5). The seeded fake PII is
treated exactly as if it were real.

## Consequences

- **Positive.** Tamper-evidence without a deprecated dependency; the chain is
  verifiable offline (`make ledger`), and the WORM export is independent of app IAM.
- **Cost.** Hash-chaining and verification are the platform's own responsibility
  rather than a managed ledger's — a small, well-contained amount of code.

## Alternatives considered

- **Amazon QLDB.** Rejected: deprecated. The DynamoDB + Object Lock composition
  reproduces the tamper-evidence property with supported services.

## References

- ARCHITECTURE.md §7.2 (supporting stores, QLDB note), §2 (P8), §13 (compliance), §15 (ADR-010)
- CLAUDE.md invariant #5
