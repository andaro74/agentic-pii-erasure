# ADR-015: AgentCore Runtime for reasoning, Lambda for the saga

- **Status:** Accepted (replaces the "Compute: ECS Fargate" row of [ADR-014](ADR-014-langgraph-owns-durability.md), which [ADR-016](ADR-016-serverless-durability.md) supersedes in full)
- **Anchors invariants:** CLAUDE.md #2 (deletion tools are called by executor nodes, not by models), #12 (the saga has no Bedrock permission)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-014](ADR-014-langgraph-owns-durability.md) put the whole graph — discovery subgraph *and* saga — on a single ECS Fargate service. That was one always-on container for a workload that is idle for weeks at a time, and it meant the component that talks to Bedrock and the component that executes irreversible deletions ran in the same process under the same task role. The boundary between them existed only as a unit test asserting that `saga/nodes/` imports no model client.

Two things changed. The platform is now AWS-only and serverless by requirement (no local mode, no always-on compute), and **Amazon Bedrock AgentCore Runtime** is generally available: a serverless agent runtime with per-session microVM isolation, built-in workload identity, a native MCP client, and support for asynchronous sessions up to 8 hours.

## Decision

Split the compute by shape, and make the split an IAM boundary.

| | Discovery (reasoning plane) | Saga (control plane) |
|---|---|---|
| **Compute** | **AgentCore Runtime** | **AWS Lambda** (`saga-executor`, `resume`, `approval`) |
| **Shape** | One long, exploratory, model-driven session per subject; unpredictable duration | Many short deterministic bursts separated by days |
| **Why it fits** | 8-hour async ceiling, session isolation per subject, scale-to-zero, native MCP + identity | Sub-second start, event sources everywhere, per-invocation billing |
| **Bedrock access** | ✅ `InvokeModel` on the pinned inference profile | ❌ **explicitly denied in the execution role** |
| **Participant access** | ❌ none — Gateway tools only | ❌ none — Gateway tools only |

Session isolation is not incidental. AgentCore Runtime gives each invocation a dedicated microVM, so one subject's discovery session cannot leak artifacts into another's through shared process state. For a component whose entire job is reading one data subject's PII, that is the correct default rather than a nice-to-have.

## Consequences

- **Positive — invariant 2 becomes enforceable.** "The saga never re-enters the model" was a code-review rule backed by an import test. It is now also an IAM denial: the `saga-executor` role has no `bedrock:*`. The test stays, because it fails faster and names the reason, but the control no longer depends on it.
- **Positive — zero idle compute.** Both planes scale to zero. The weeks a saga spends parked cost nothing but DynamoDB storage.
- **Positive — the planes can be upgraded independently.** A new discovery container image does not redeploy the saga, and vice versa.
- **Cost 1 — the 15-minute Lambda ceiling is real.** The executor drives the graph from the current checkpoint until the next `interrupt()` or `END`. Each node's work is a bounded Gateway call and the participant set is bounded by the manifest, so realistic phases complete in seconds. A 200-participant tenant would not. A `saga.executor_timeout` metric exists and ARCHITECTURE §16 Q5 keeps the question open rather than pretending the ceiling does not exist.
- **Cost 2 — two deployment artifacts.** A container image in ECR for the Runtime, zipped/imaged Lambdas for the control plane. `infra/` owns both; `make deploy-dev` does both.
- **Cost 3 — cold-start latency on resume.** A resume after 30 days is a cold Lambda plus a cold DynamoDB read. Irrelevant against a 30-day window; worth stating so nobody benchmarks it as a hot path.

## Alternatives considered

- **Everything on AgentCore Runtime.** Rejected: the saga would then run in the one place that holds Bedrock credentials, giving up the IAM claim that is the point of this ADR. The 8-hour ceiling would also still not hold a 30-day pause, so it buys nothing durability-wise.
- **Everything on Lambda, including discovery.** Rejected: a model-driven session with unpredictable tool-call depth against a 15-minute wall is a recipe for truncated discovery — and truncated discovery is a recall failure, which is the one failure mode [ADR-008](ADR-008-recall-1.0-hard-gate.md) exists to prevent.
- **ECS Fargate** ([ADR-014](ADR-014-langgraph-owns-durability.md)). Rejected: always-on compute for a workload that is idle by design, and no plane separation.

## References

- ARCHITECTURE.md §3.2 (why Lambda and Runtime), §9.3 (the IAM matrix), §16 Q5 (the ceiling)
- [ADR-016](ADR-016-serverless-durability.md) (what holds the pause) · [ADR-018](ADR-018-agentcore-policy.md) (what the identities may call)
