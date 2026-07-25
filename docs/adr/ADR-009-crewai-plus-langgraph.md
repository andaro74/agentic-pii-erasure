# ADR-009: CrewAI for discovery + LangGraph for the saga

- **Status:** **Superseded by [ADR-011](ADR-011-strands-single-framework.md)**
- **Baseline:** architecture v0.1 (first of three framework decisions — kept deliberately)

## Context

Two workloads with opposite shapes:

| | Discovery | Execution |
|---|---|---|
| Shape | Divergent, parallel fan-out | Convergent, ordered, interruptible |
| Needs | Multi-agent crew, open-ended search | Explicit edges, `interrupt()`, deterministic replay |

## Decision (as it stood)

Use the best tool for each shape: **CrewAI** for the divergent discovery crew
(cartographer, prospector, lineage, counsel, editor), and **LangGraph** for the
convergent saga with its checkpointer and interrupts.

## Why it was superseded

A dual-framework stack means two dependency sets, two mental models, and a seam
between them exactly where the signed manifest is handed off. The divergent/convergent
*analysis* was sound and has outlived every framework change — but splitting it across
two frameworks was more machinery than the seam justified. [ADR-011](ADR-011-strands-single-framework.md)
attempted to collapse to a single framework (Strands); [ADR-013](ADR-013-langgraph-single-framework.md)
settled it on LangGraph once LangChain 1.0 middleware closed the interception gap.

## The decision that survived all three changes

Discovery is divergent and read-only; execution is convergent and deterministic.
Only the *implementation* moved — CrewAI+LangGraph → Strands → LangGraph. That the
big call changed twice, on the record, is the point of keeping this ADR.

## References

- ARCHITECTURE.md §6.2 (framework roles), §15 (ADR-009)
- Superseded by [ADR-011](ADR-011-strands-single-framework.md) → [ADR-013](ADR-013-langgraph-single-framework.md)
