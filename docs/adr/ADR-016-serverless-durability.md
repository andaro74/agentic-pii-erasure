# ADR-016: DynamoDB checkpointer and EventBridge Scheduler own durability

- **Status:** Accepted (supersedes [ADR-014](ADR-014-langgraph-owns-durability.md); durability chain [003](ADR-003-step-functions-owns-durability.md) → [014](ADR-014-langgraph-owns-durability.md) → 016)
- **Anchors invariants:** CLAUDE.md #9 (never widen the pinned durability constraint), #11 (resume handlers must be idempotent)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-014](ADR-014-langgraph-owns-durability.md) established the right principle — **the checkpointer is the system of record, not a cache** — and the wrong storage for a serverless platform. It put checkpoints on Aurora PostgreSQL Serverless v2 via `langgraph-checkpoint-postgres`, with the graph running on an always-on Fargate service.

In an otherwise serverless architecture, Aurora is the outlier: a cluster to operate, a VPC that every Lambda touching state must attach to (adding ENI cold starts), cold-resume latency when scaled to zero ACU, and idle cost across the weeks a saga spends parked at the approval gate. None of that buys anything the workload needs.

The pause itself is the hard constraint and nothing serverless can hold it as a process:

| Candidate | Ceiling |
|---|---|
| Lambda invocation | 15 minutes |
| AgentCore Runtime session | 8 hours async, 15 minutes sync |
| Approval gate + grace window | **days to weeks** |

## Decision

**The pause is data, not a process.** A node calls `interrupt()`, LangGraph writes a checkpoint, and the Lambda returns. Days later an EventBridge Scheduler one-shot schedule fires a resume Lambda, which loads the checkpoint and calls `Command(resume=…)` in a different process on different hardware.

| Concern | Mechanism |
|---|---|
| Durable pause | `interrupt()` + checkpointer |
| **State store** | **`langgraph-checkpoint-aws` `DynamoDBSaver`**, on-demand table, S3 offload above 350 KB, TTL for expiry |
| Compute | `saga-executor` Lambda ([ADR-015](ADR-015-serverless-compute-split.md)) |
| **Wall-clock timers** | **EventBridge Scheduler one-shot → resume Lambda** |
| Retries | LangGraph node retry policies + Lambda async retry + SQS DLQ |

The `thread_id` **is** the `sagaId`, so checkpoint history, AgentCore Observability traces, and ledger entries join with no custom plumbing.

Nothing may hold saga state outside the checkpointer. That rule is unchanged from ADR-014 and is the reason this is an ADR rather than a config change.

## Consequences

- **Positive — zero idle cost and no VPC.** DynamoDB on-demand charges for storage and requests only. No cluster, no ENI attachment, no scaled-to-zero cold-start penalty on the state store itself.
- **Positive — resume is genuinely stateless.** Any Lambda in any AZ can pick up any thread. There is no leader, no session affinity, and no warm process to lose.
- **Positive — S3 offload removes the item-size cliff.** A manifest with hundreds of artifacts would blow DynamoDB's 400 KB item limit; the saver offloads payloads above 350 KB to S3 transparently.
- **Cost 1 — serialization moved to a younger package.** ADR-014 rejected a DynamoDB checkpointer precisely to keep serialization inside the widely-used Postgres saver. That trade is now reversed deliberately: `DynamoDBSaver` ships in `langgraph-checkpoint-aws`, maintained by AWS in the `langchain-aws` repo, and it is younger. **Invariant #9 therefore extends to it** — `langgraph` *and* `langgraph-checkpoint-aws` are pinned to exact versions and move in lockstep, gated by the upgrade canary. ARCHITECTURE §16 Q6 keeps "is the canary sufficient, or do we need a version-tagged checkpoint envelope?" open.
- **Cost 2 — checkpoint compatibility across a long pause is unchanged and still the worst failure mode in the system.** With a 30-day grace window, in-flight state spans framework versions at all times. A serialization change mid-window makes `resume` fail to deserialize a checkpoint written by the old version, stranding a live erasure request **silently, past a statutory deadline**.
- **Cost 3 — timers are still ours.** Step Functions' `Wait` state handled 30-day windows natively; we build on EventBridge Scheduler and own exactly-once resume. See invariant #11.

## Controls on cost 2

1. **Exact version pins (invariant #9).** `pyproject.toml` pins `langgraph` and `langgraph-checkpoint-aws` to *exact* versions, not ranges. A lockfile for the transitive layer is a ROADMAP M0 deliverable — until it is committed, only the two direct pins are protected, and pyproject.toml says so rather than claiming otherwise. **Never widen this constraint.**
2. **The upgrade canary — the only control that actually catches a stranded saga.** `make upgrade-canary` is required before any bump: pause a saga, upgrade the pinned packages, assert a clean resume against the same DynamoDB table.
3. **Idempotent resume (invariant #11).** EventBridge Scheduler delivers at least once; a duplicate resume of a phase-3 node is a duplicate deletion attempt. `scheduler/handler.py` is idempotent per `(thread_id, wake_reason)` and must not rely on participant idempotency keys as the *only* defence.
4. **`checkpoint.resume_failure` alarms on any occurrence.** It is an upgrade defect, never a participant defect, and it is P1.

These reduce the risk; they do not remove it.

## Alternatives considered

- **Aurora Serverless v2, min 0 ACU** ([ADR-014](ADR-014-langgraph-owns-durability.md)). Rejected: keeps the mature Postgres saver, but drags a VPC and a cluster into an otherwise serverless stack for state that is a key-value read by `thread_id`. The smallest change, and the wrong one.
- **`AgentCoreMemorySaver`** (also in `langgraph-checkpoint-aws`). Rejected: AgentCore Memory is built for conversational context and cross-session learning, and checkpoints legitimately contain a manifest full of artifact locators. Putting them in Memory collides head-on with [ADR-019](ADR-019-agentcore-memory-priors.md)'s "topology only, never subject data" rule and threat T7. Two stores, two purposes, no ambiguity about which one may hold subject-shaped data.
- **Step Functions** ([ADR-003](ADR-003-step-functions-owns-durability.md)). Still rejected for the original reason — two orchestrators with a claim on "what happens next" — and still a defensible fork, which is why ADR-003 is kept.
- **`ValkeySaver` on ElastiCache Serverless.** Rejected: fastest, but a cache-shaped store as the system of record for a 30-day-durable legal obligation is the wrong risk posture.

## References

- ARCHITECTURE.md §6.1 (the durability problem), §7.2 (stores), §10.1 (`checkpoint.resume_failure`), §12 (failure matrix), §16 Q6
- pyproject.toml (the pins) · CLAUDE.md invariants #9, #11
- Supersedes [ADR-014](ADR-014-langgraph-owns-durability.md) · Compute split: [ADR-015](ADR-015-serverless-compute-split.md)
