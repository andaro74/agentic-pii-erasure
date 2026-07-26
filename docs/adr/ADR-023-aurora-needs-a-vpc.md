# ADR-023 — Aurora needs a VPC; the platform still never enters one

**Status:** Accepted · 2026-07-26
**Clarifies:** [ADR-015](ADR-015-serverless-compute-split.md), [ADR-016](ADR-016-serverless-durability.md), [ADR-017](ADR-017-real-aws-participants.md)

## Context

Seven places in this repository stated that the platform uses **no VPC** — `README.md`,
`ARCHITECTURE.md` twice, `PROJECT-STRUCTURE.md`, `CLAUDE.md` twice, and ADR-016. The claim
was written when the participants were simulated ([ADR-012](ADR-012-simulated-participants.md)),
where it was true, and it survived the rewrite to real services ([ADR-017](ADR-017-real-aws-participants.md))
without being re-checked against them.

It is not true. **Aurora Serverless v2 cannot exist outside a VPC.** A cluster requires a
DB subnet group, a subnet group requires subnets, and subnets require a VPC. CDK rejects
`DatabaseCluster` without one at synth time, which is how this surfaced (V8-4) — while
building M4's `billing-ledger`, not while reading the sentence that asserted otherwise.

There is no VPC-less Aurora to switch to. Aurora DSQL is a different engine with different
semantics, and adopting it to preserve a sentence would be choosing the architecture to fit
the documentation.

## Decision

**Keep Aurora. Correct the claim. Make the true, enforceable property explicit.**

The property that carries the engineering weight was never "there is no VPC in the account".
It was:

> **Nothing this platform runs attaches to a VPC.**

That is what avoids ENI cold starts on every Lambda, avoids NAT gateway charges, keeps the
saga's cold path fast, and lets `cdk synth` assert something falsifiable. It remains true
with Aurora present, because the RDS Data API is a public SigV4 endpoint — the participant
Lambda talks to `rds-data.<region>.amazonaws.com`, not to anything inside the network.

The VPC that exists is scoped to hold the cluster and nothing else:

| Component | Present | Why |
|---|---|---|
| Isolated subnets across 2 AZs | ✅ | Aurora's minimum. Subnets are free. |
| NAT gateway | ❌ | ~$32/month for existing. Forbidden by the platform's cost rule. |
| Internet gateway | ❌ | Nothing in the VPC makes outbound calls. |
| VPC endpoints | ❌ | Nothing in the VPC calls AWS APIs — the Lambda is outside it. |
| Lambda attachments | ❌ | Asserted at synth time. This is the invariant. |

An idle VPC configured this way bills **nothing**, so [ADR-021](ADR-021-s3-vectors-for-cost.md)'s
rule — no component may bill continuously for existing — is not violated.

## Consequences

- **The synth assertion stays, and now means what it says.** "No Lambda carries a
  `VpcConfig`" is checkable and checked. "No VPC exists" was neither.
- **Documentation corrected in seven places** rather than deleted, per the repo's rule that
  a resolved uncertainty is marked resolved on the record.
- **A cost note the previous claim hid.** A VPC is free; the things usually put *in* one are
  not. Stating "no VPC" concealed the fact that the expensive parts are NAT gateways and
  endpoints, which is the distinction someone copying this design actually needs.
- **`make destroy-dev` gains a dependency order.** The VPC cannot be deleted until the
  cluster is, which CloudFormation handles, but it lengthens teardown.
- **The negative result is worth more than the claim was.** "We are fully serverless with no
  VPC" is a common architectural aspiration, and the specific reason it fails — one managed
  database with no VPC-less variant — is more useful to a reader than the aspiration.

## Alternatives considered

- **Swap Aurora for DynamoDB and drop the RELATIONAL archetype.** Rejected: referential
  integrity ordering and statutory-retention conflict are exactly what that archetype
  teaches, and no NoSQL store reproduces them. Deleting an archetype to preserve a sentence
  is the tail wagging the dog.
- **Aurora DSQL, which is VPC-less.** Rejected for now: a different engine with different
  transactional semantics, adopted for documentation convenience rather than on its merits.
  Worth revisiting on its own terms, in its own ADR.
- **Quietly add the VPC and leave the docs alone.** Rejected. This is the failure mode
  `docs/VALIDATION.md` exists to catch, and doing it deliberately would be worse than the
  accident that produced the original claim.
