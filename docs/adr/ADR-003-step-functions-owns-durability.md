# ADR-003: Step Functions owns durability

- **Status:** **Superseded by [ADR-014](ADR-014-langgraph-owns-durability.md)**
- **Baseline:** architecture v0.1 (kept deliberately — the fork is still defensible)

## Context

The approval gate pauses for days to weeks. Something durable has to hold the pause
and the wall-clock timers (grace window, T+7/T+30 sweeps) without keeping an agent
process warm across a human's deliberation.

## Decision (as it stood)

AWS Step Functions owns durability. It holds a **task token** across the pause, its
`Wait` state handles 30-day windows natively, and the agent framework runs inside
bounded Step Functions invocations. The checkpointer, if any, is a cache — Step
Functions is the system of record.

## Why it was superseded

Running two orchestrators (Step Functions *and* the graph framework) created a
divergence tiebreaker problem: two systems with a claim on "what happens next," and
saga logic split across a state machine definition and Python. [ADR-014](ADR-014-langgraph-owns-durability.md)
collapses this to one orchestrator — LangGraph checkpointers as the system of
record — which makes phase ordering, compensation, and hold re-evaluation
**unit-testable in plain Python**.

## What it cost to walk away (recorded honestly)

Step Functions gave two things away for free that ADR-014 now has to build and own:

1. **Wall-clock timers.** The `Wait` state handled 30-day windows natively;
   [ADR-014](ADR-014-langgraph-owns-durability.md) rebuilds them on EventBridge
   Scheduler → resume Lambda.
2. **Checkpoint compatibility across a long pause** was a non-issue when AWS owned
   the state; it becomes an operational constraint once the framework owns
   serialization.

**This fork remains defensible.** If the timer burden proves unsustainable, the
documented reversal is LangGraph Platform or a return to this path — decided
deliberately via a new ADR, never drifted into as a hybrid.

## References

- ARCHITECTURE.md §6.1 (durability), §15 (ADR-003), §16 Q5 (timer burden)
- Superseded by [ADR-014](ADR-014-langgraph-owns-durability.md)
