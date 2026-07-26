# infra/ — the deployment is the product

> ## ⚠️ Read this before you deploy
>
> **This stack costs real money — but nothing in it bills continuously for existing rather than for working.** Every component is per-request, per-GB, or per-session-second. An idle dev stack costs cents per month, and Bedrock tokens are the largest line item on an active one.
>
> That is a deliberate constraint, and it cost one participant its original service: `vector-index` was Amazon OpenSearch Serverless, whose OCU floor is charged for as long as the collection exists and which used to dominate this bill by an order of magnitude over everything else combined. It is now **S3 Vectors**, priced on stored bytes and requests with no provisioned capacity. **The swap was made purely on cost** — [ADR-021](../docs/adr/ADR-021-s3-vectors-for-cost.md).
>
> **The remaining hazard is not a price — it is a bucket you cannot delete.** See the next section before you touch the Object Lock retention parameter.
>
> ```bash
> make destroy-dev   # still run this when you are done
> ```

There is no local mode. `cdk deploy` is the entry point, by decision — see [ADR-017](../docs/adr/ADR-017-real-aws-participants.md).

**Before adding any AWS service to this stack, check whether it has a provisioned floor.** If it does, it needs an ADR arguing why the floor is worth it — that is the rule ADR-021 established, and it is cheaper to apply at design time than after a month of billing.

---

## Teardown, and the one thing that blocks it

`make destroy-dev` removes the stack. **One resource can refuse to go: the `compliance-archive` bucket.**

S3 Object Lock in COMPLIANCE mode means an object cannot be deleted by anyone — including the account root — until its retention period expires. That is the whole point of archetype 5, and it applies to your teardown exactly as it applies to an erasure request.

Dev stacks therefore deploy with a **short Object Lock retention period** (a stack parameter, measured in days). If you raise it to something production-shaped, you have created a bucket you cannot delete until that period elapses, and no amount of IAM will help. Check the parameter before you deploy, not after.

## Cost shape

| Component | Idle cost | Notes |
|---|---|---|
| **S3 Vectors** | **≈ zero** | Storage per GB-month + per-request. Replaced OpenSearch Serverless purely on cost — [ADR-021](../docs/adr/ADR-021-s3-vectors-for-cost.md) |
| Aurora Serverless v2 | ≈ zero compute | `min_capacity = 0` ACU — scales to zero, pays cold-resume latency instead. Storage still bills. |
| Lambda · DynamoDB on-demand · EventBridge Scheduler · S3 · KMS · SES · Cognito · Glue/Athena | ≈ zero | Per-request or per-GB |
| AgentCore Runtime | ≈ zero | Per session-second; scales to zero |
| Amazon Bedrock | Per-token | Discovery is the only model spend, and the largest line item on an active stack |

ARCHITECTURE §16 Q7 used to ask whether teaching the derived-index archetype was worth a continuous charge. [ADR-021](../docs/adr/ADR-021-s3-vectors-for-cost.md) answered it: no, and it did not have to be. The archetype survived the move to S3 Vectors with a sharper lesson — no delete-by-query, and an embedding that is itself personal data.

## Prerequisites

- An AWS account and credentials with permission to create the resources below.
- **Amazon Bedrock model access enabled** for your chosen Claude model in your region. Bedrock requires per-account, per-model opt-in; a deploy will succeed and discovery will then fail at runtime if you skip it.
- **AgentCore availability in your region.** Check before choosing a region — the stack is regional and cross-region AgentCore is not a supported topology here.
- Node (for the CDK CLI) and the repo's Python venv (`make install`).

## Stacks

```
infra/
├── app.py
└── stacks/
    ├── foundation.py      KMS CMK · DynamoDB tables (checkpoints, ledger, tombstones,
    │                      DEK registry, idempotency) · S3 buckets · EventBridge bus
    ├── participants.py    the 8 real services + their Lambda handlers, one role each
    ├── gateway.py         AgentCore Gateway · targets · Policy attachment · Identity
    ├── runtime.py         AgentCore Runtime for discovery · ECR image · Memory store
    ├── saga.py            saga-executor + resume Lambdas · Scheduler role · SQS DLQ
    ├── api.py             HTTP API + Cognito authorizer for intake, approval, operator reads
    └── observability.py   alarms and dashboards for ARCHITECTURE §10.1
```

## Assertions that run at synth time

`cdk synth` is part of `make check`, so these run on every commit with no AWS account. They live here rather than in a runtime test because a runtime test would find them too late:

| Assertion | Invariant | Why here |
|---|---|---|
| `saga-executor` role has no `bedrock:*` action | 12 | "The saga cannot re-enter the model" is an IAM fact, not a convention |
| DEK registry table has PITR **disabled** and no AWS Backup selection | 14 | A restore un-shreds every subject deleted since the restore point (threat T9) |
| No Lambda has a VPC configuration | — | Aurora is reached via the RDS Data API precisely so none needs one |
| Ledger archive bucket has Object Lock in COMPLIANCE mode | — | A mutable audit ledger is not an audit ledger |
| Discovery Runtime role has no participant-service actions | 2 / P2 | The least-privileged plane stays least-privileged |

If one of these fails, the fix is the stack — never the assertion.

## Deploying

```bash
make synth          # safe, free, no credentials needed
make deploy-dev     # ⚠️ human-only: creates infrastructure, spends money
make destroy-dev    # do this
```

`make deploy-dev`, `make deploy`, and `make destroy-dev` are denied to Claude Code in `.claude/settings.json`. They mutate real infrastructure and spend real money, so a human runs them, deliberately.

Policy starts in **`LOG_ONLY`** mode. Flip to `ENFORCING` only after the evaluation corpus produces an empty deny set against known-good trajectories — ARCHITECTURE §9.4. The mode is a stack parameter rather than an environment variable, so the flip is a deploy and shows up in CloudTrail.
