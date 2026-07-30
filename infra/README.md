# infra/ — the deployment is the product

> ## ⚠️ Read this before you deploy
>
> **This stack costs real money — but almost nothing in it bills continuously for existing rather than for working.** Every component is per-request, per-GB, or per-session-second, with **one exception, stated rather than glossed: a single Secrets Manager secret at $0.40/month.** The RDS Data API authenticates with a secret ARN, and the Data API is what keeps every Lambda out of a VPC — so the floor is forced by the choice that removes a much larger one. An idle dev stack costs cents per month, and Bedrock tokens are the largest line item on an active one.
>
> That claim is now checked rather than asserted: `tests/unit/test_cost_floors.py` reads the synthesised templates and fails on any floor-bearing resource type without an ADR behind it — including services this repo has never used, because the point is the *next* one somebody adds ([V13-4](../docs/VALIDATION.md)).
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

## Cost guardrails — deploy once per account, and not with `--all`

```bash
make deploy-guardrails      # ⚠️ human-only. Once per ACCOUNT, not per stage.
```

Two budgets and an SNS topic, in `asdp-account-guardrails` — the one stack with **no stage in its name**. A MONTHLY budget with a *forecasted* alert (the only kind that arrives while the spend can still be prevented) and a DAILY one with an actual alert, which is what catches the failure this architecture is actually exposed to: a stack somebody left up. Monitoring budgets are free — AWS charges only for *action-enabled* ones, and these take no actions, both because of the cost and because an action that stops resources to save money could interrupt an in-flight erasure or its audit trail.

**It is built only behind a context flag, and that is load-bearing rather than fussy.** A budget is account-wide. `make deploy-dev` runs `cdk deploy --all` and `make destroy-dev` runs `cdk destroy --all`, and CI runs both against an ephemeral `pr-<run_id>` stage — so a budget reachable from `--all` would be created once per pull request and **deleted on teardown**. A green build would silently disarm the account's cost guardrail. `tests/unit/test_guardrails_synth.py` asserts against the synthesised templates that no stage stack contains a budget.

**One manual step remains, and nothing is alerting until you take it:** subscribe someone to the topic. The stack creates no subscription on purpose — who gets paged is an operational decision, and a stack that mailed a hardcoded address would be one nobody else could deploy. The topic ARN is a stack output. Budgets also sends a confirmation email that the recipient must accept.

**Do not enable encryption on that topic.** AWS Budgets cannot publish to an SSE topic without additional KMS grants; the Budgets troubleshooting guide's own remedy is to disable encryption. Adding a key would stop the alerts silently. No budget alert carries subject data, so invariant 5 is not what is being traded.

A per-stage budget scoped by tag is not an option, and the reason is worth knowing: a cost-allocation tag has to be **activated by hand in the Billing console** before any budget can filter on it. Until then the filter matches nothing, and the budget reports $0.00 forever — a control that reads exactly like an account which is not spending money.

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

- An AWS account and credentials with permission to create the resources below, reachable by the AWS CLI (`aws sts get-caller-identity` must succeed).
- **Amazon Bedrock model access enabled** for your chosen Claude model in your region. Bedrock requires per-account, per-model opt-in; a deploy will succeed and discovery will then fail at runtime if you skip it.
- **AgentCore availability in your region.** Check before choosing a region — the stack is regional and cross-region AgentCore is not a supported topology here.
- Node (for the CDK CLI, fetched by `npx` at a pinned version) and the repo's Python venv (`make install`).

## First run, in order

Each step names the symptom you get by skipping it, because most of them do not fail where you made the mistake.

| # | Step | Skip it and you get |
|---|---|---|
| 1 | Configure AWS credentials (`aws configure` / SSO / a profile) | `Unable to locate credentials` at step 6 |
| 2 | `make install` — venv, pinned deps, and a `.env` copied from `.env.example` | `make check` cannot run |
| 3 | Edit `.env`: set **`AWS_REGION`** to a region where AgentCore *and* your model are available | The make targets refuse to run and tell you so — deliberately; they do **not** fall back to your profile's region |
| 4 | Enable Bedrock model access in that region, then put the inference profile ID in `PII_ERASURE_MODEL_ID` (`aws bedrock list-inference-profiles`) | A clean deploy, then a discovery failure at M7 |
| 5 | `make check` — hermetic, no AWS account touched | Nothing; this is the free confidence check |
| 6 | **`make bootstrap`** — one-time per account **and** region | `Environment aws://…/… has not been bootstrapped` at step 7 |
| 7 | **`make deploy-guardrails`** — one-time per account. Free | No cost alerting. A stack left up, or a runaway, is discovered on next month's bill |
| 8 | Subscribe to the `asdp-account-budget-alerts` topic and confirm the email | The budgets exist and alert nobody |
| 9 | `make deploy-dev` | — |
| 10 | `make destroy-dev` when you are done | See *Teardown* above |

`make bootstrap` resolves your account from `aws sts get-caller-identity` and the region from `.env`, then creates the `CDKToolkit` stack (a staging bucket, an ECR repository, and the deploy roles). It is idempotent — re-running it is harmless — and it is **per region**: change `AWS_REGION` later and you bootstrap again.

Steps 6–10 mutate real infrastructure, and most of them spend money, so they are denied to Claude Code in `.claude/settings.json` and run by a human. Step 7 is the exception on cost — monitoring budgets are free — but it is still a deploy, so it is still yours.

> **`.env` is read by these targets, and your shell wins.** `make` does not read `.env` on its own; the AWS-touching targets source it explicitly, and any variable already exported in your shell overrides the file. That is how CI supplies `AWS_REGION` and a per-run `PII_ERASURE_STAGE` without a `.env` at all. `make synth` deliberately does *not* load it — synth needs no region and no credentials, and it stays that way because it is part of the hermetic gate.

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
    ├── observability.py   alarms and dashboards for ARCHITECTURE §10.1
    └── guardrails.py      account-wide cost budgets. NOT stage-scoped, NOT in `--all`
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
make synth              # safe, free, no credentials needed, no .env needed
make bootstrap          # ⚠️ human-only: once per account + region
make deploy-guardrails  # ⚠️ human-only: once per account. Cost budgets; free
make deploy-dev         # ⚠️ human-only: creates infrastructure, spends money
make destroy-dev        # do this
```

`deploy-dev` and `destroy-dev` act on `STAGE`, which resolves in this order: `make deploy-dev STAGE=foo` → `PII_ERASURE_STAGE` in your shell → `PII_ERASURE_STAGE` in `.env` → `dev`. Both echo the stage and region before they act; read that line.

`make deploy-dev`, `make deploy`, and `make destroy-dev` are denied to Claude Code in `.claude/settings.json`. They mutate real infrastructure and spend real money, so a human runs them, deliberately.

Policy starts in **`LOG_ONLY`** mode. Flip to `ENFORCING` only after the evaluation corpus produces an empty deny set against known-good trajectories — ARCHITECTURE §9.4. The mode is a stack parameter rather than an environment variable, so the flip is a deploy and shows up in CloudTrail.
