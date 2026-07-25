# ADR-014: LangGraph checkpointers own durability

- **Status:** Accepted (supersedes [ADR-003](ADR-003-step-functions-owns-durability.md))
- **Anchors invariants:** CLAUDE.md #9 (never widen the langgraph version constraint), #11 (resume handlers must be idempotent)
- **Baseline:** architecture v0.1

## Context

Approval takes days, realistically weeks. No agent process may be held warm across a
human's deliberation — it is a token-budget disaster and it turns the pause into a
liveness dependency. Something durable must hold the paused saga and its wall-clock
timers (grace window, T+7/T+30 sweeps).

[ADR-003](ADR-003-step-functions-owns-durability.md) had Step Functions own this,
with the framework running inside bounded invocations. That meant **two
orchestrators** with a claim on "what happens next," and saga logic split between a
state-machine definition and Python.

## Decision

**LangGraph checkpointers are the system of record** — not a cache, not an
optimisation. A node calls `interrupt()`, state is checkpointed, and the process
**exits**. Days later, `Command(resume=…)` reconstitutes the graph exactly where it
stopped, in a different process on a different host. Nothing may hold saga state
outside the checkpointer.

| Concern | Mechanism |
|---|---|
| Durable pause | `interrupt()` + checkpointer |
| State store | `langgraph-checkpoint-postgres` on Aurora Serverless v2 (SQLite locally) |
| Compute | ECS Fargate service |
| **Wall-clock timers** | **EventBridge Scheduler → resume Lambda** (in-process asyncio locally) |
| Retries | LangGraph node retry policies |

The `thread_id` **is** the `sagaId`, so checkpoint history, traces, and ledger
entries join with no custom plumbing.

## Consequences

- **Positive.** One orchestrator removes the divergence tiebreaker. Phase ordering,
  compensation, and hold re-evaluation become **unit-testable in plain Python**.
- **Cost 1 — timers are ours now.** Step Functions' `Wait` state handled 30-day
  windows natively; we build them on EventBridge Scheduler + a resume Lambda. §16 Q5
  keeps "is the timer burden sustainable?" open, with LangGraph Platform as the named
  fallback — to be chosen deliberately, never drifted into as a hybrid.
- **Cost 2 — checkpoint compatibility across a long pause.** With a 30-day grace
  window, in-flight state spans framework versions *at all times*. A serialization
  change mid-window makes `resume` fail to deserialize a checkpoint written by the
  old version — stranding a live erasure request **silently, past a statutory
  deadline**. This is the failure mode with the worst blast radius in the system.

## Controls on the second cost

1. **Exact version pin (invariant #9).** `pyproject.toml` pins `langgraph` and the
   checkpoint packages to *exact* versions, not ranges — serialization lives in the
   checkpoint packages as much as in `langgraph`, so they move in lockstep. A
   committed lockfile covers the transitive layer. **Never widen this constraint.**
2. **The upgrade canary (the only control that actually catches a stranded saga).**
   `make upgrade-canary` is required before any bump: **pause a saga, upgrade the
   framework, assert a clean resume** (`scripts/upgrade_canary.sh`, `CANARY_STAGE=pause|resume`).
3. **Idempotent resume (invariant #11).** EventBridge Scheduler can deliver more than
   once; a duplicate resume of a phase-3 node is a duplicate deletion attempt.
   `scheduler/handler.py` is idempotent per `(thread_id, wake_reason)` and must not
   rely on participant idempotency keys as the *only* defence.

These reduce the risk; they do not remove it. The README states this plainly, and
[ADR-003](ADR-003-step-functions-owns-durability.md) is kept because its fork —
letting AWS own durability — remains defensible.

## Alternatives considered

- **Step Functions** ([ADR-003](ADR-003-step-functions-owns-durability.md)). Rejected:
  two orchestrators; saga not unit-testable in Python.
- **LangGraph Platform.** Not chosen now, but the named fallback if the self-owned
  timer burden proves unsustainable (§16 Q5).
- **DynamoDB checkpointer.** Rejected: Aurora Serverless v2 with the maintained
  Postgres checkpointer keeps serialization inside the pinned, canary-gated packages.

## References

- ARCHITECTURE.md §6.1 (durability), §10.1 (`checkpoint.resume_failure`), §12 (failure matrix), §15 (ADR-014), §16 Q5
- pyproject.toml (the pin) · CLAUDE.md invariants #9, #11 · Supersedes [ADR-003](ADR-003-step-functions-owns-durability.md)
