# ADR-011: Strands as the single framework

- **Status:** **Superseded by [ADR-013](ADR-013-langgraph-single-framework.md)**
- **Baseline:** architecture v0.1 (second of three framework decisions — kept deliberately)

## Context

[ADR-009](ADR-009-crewai-plus-langgraph.md) ran two frameworks — CrewAI for
discovery, LangGraph for the saga — with a seam between them at the manifest
handoff. The goal became: one framework, one dependency set, one mental model,
spanning both the divergent discovery shape and the convergent execution shape.

## Decision (as it stood)

Adopt **Strands** as the single framework for both discovery and the saga.

## Why it was superseded

Two things moved the decision again:

1. **LangChain 1.0 shipped middleware** that wraps every tool call — the exact
   in-process policy interception point this architecture needs (§9.1). That closed
   the capability gap that had made a LangChain-native single stack unattractive.
2. **Durability was moving in-framework** ([ADR-014](ADR-014-langgraph-owns-durability.md)):
   the checkpointer becomes the system of record, and LangGraph's checkpoint/interrupt/
   resume model is the most mature fit for a saga that pauses for weeks and must
   resume by deserializing old state.

[ADR-013](ADR-013-langgraph-single-framework.md) settles on LangGraph. The
convergent/divergent split from [ADR-009](ADR-009-crewai-plus-langgraph.md) still
holds; again, only the implementation moved.

## References

- ARCHITECTURE.md §6.2 (framework roles), §15 (ADR-011)
- Supersedes [ADR-009](ADR-009-crewai-plus-langgraph.md) · Superseded by [ADR-013](ADR-013-langgraph-single-framework.md)
