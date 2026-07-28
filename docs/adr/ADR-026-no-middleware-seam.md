# ADR-026: There is no middleware seam, because the model holds no tools

- **Status:** Accepted — removes `policy/middleware.py` from the build and from invariant 0's allowlist. Refines [ADR-013](ADR-013-langgraph-single-framework.md) (whose rationale cited middleware) and [ADR-018](ADR-018-agentcore-policy.md) (which made the Gateway authoritative)
- **Anchors invariants:** CLAUDE.md #0 (the framework boundary is an explicit allowlist), #1 (the discovery agent never gets a mutating tool)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

`policy/middleware.py` has been in the plan since the framework was chosen. [ADR-013](ADR-013-langgraph-single-framework.md) picked LangGraph over Strands partly *because* "LangChain 1.0 middleware closed the interception gap" — a per-tool-call hook where an in-process Cedar decision could run before the call left the process. It was listed in M6's **Build**, and PROJECT-STRUCTURE promised it "lands at M7".

M6 shipped without it. M7 shipped without it. Neither was a scheduling slip, and calling it one twice is what makes this ADR necessary.

**The seam it attaches to does not exist.** LangChain middleware wraps the tool calls an *agent* makes — `create_agent` with bound tools, model decides, middleware intercepts. The discovery subgraph is not that shape. Its nodes are deterministic Python that call `GatewayToolset.call()` directly; the one model in the platform ([`discovery/advisor.py`](../../src/pii_erasure/discovery/advisor.py)) holds **no tools at all** and returns a list of scope hints. The entire `src/` tree contains exactly one `langchain*` import — `ChatBedrockConverse` — and no `create_agent`, no `bind_tools`, no `AgentMiddleware`.

There is nothing between a model and a tool call to intercept, because there is no path from a model to a tool call.

## Decision

**`policy/middleware.py` is not built, and `policy/middleware.py` comes out of invariant 0's framework allowlist.**

The interception gap ADR-013 worried about is closed, but by a different and better mechanism than the one anticipated:

| Where a tool call could go wrong | What actually stops it |
|---|---|
| A model chooses a mutating verb | It cannot — the model is never offered a tool (invariant 1, `advisor.py`) |
| A caller invokes a mutating verb as discovery | AgentCore Policy denies at the Gateway; `PartiallyAuthorizeActions` filters the tool from the list first (ADR-018) |
| The tool list drifts to include one | Asserted at subgraph construction, and measured deployed by `tool_surface_minimality` |

An in-process pre-check would have been a *fourth* place, and the weakest of the four: bypassable by any caller that forgets it, which is the property PROJECT-STRUCTURE already names as the reason the Gateway is authoritative.

`policy/{engine,schema,decisions}.py` stay. They are the divergence surface — the same rules evaluated against a second backend so `make policy-test` can prove the deployed Cedar and our reading of it agree — and they are honestly labelled as that rather than as a runtime control.

## Consequences

- **Positive — the allowlist narrows.** Invariant 0 is an allowlist, and a permitted path for a file that will never exist is decoration. `tests/unit/test_import_boundary.py` names the list verbatim; it now names five entries instead of six. Widening it back is an architectural decision, which is the point of the invariant.
- **Positive — one fewer place claiming to be a control.** This repo has caught controls that could not fire four times (VALIDATION.md). A middleware file written for a seam nothing attaches to would have been the fifth, and the most convincing, because it would have had tests.
- **Cost 1 — the fast in-process pre-check is gone as a runtime concept.** Every tool call pays a Gateway round-trip for its authorization decision. That is one network hop on a path that is already a network hop, so the cost is latency that was always going to be paid.
- **Cost 2 — if a future component *does* run an agent with bound tools, this decision must be revisited before it ships**, not after. That component would reintroduce exactly the seam ADR-013 described, and it would need middleware, an allowlist entry, and a superseding ADR. The trigger is "an agent gets tools", and it is worth watching for because it will arrive as a feature request, not as an architecture question.

## Alternatives considered

- **Build it anyway, unused, so the seam exists when needed.** Rejected: a control with no caller is the defect class this repo's validation discipline exists to catch, and an unused file with passing tests reads to a reviewer as a live control.
- **Defer it to M8.** Rejected — this was the third deferral, and the reason was never schedule. Deferring again would have recorded a false one.
- **Give the advisor tools and let middleware police them.** Rejected, and firmly: it inverts invariant 1. The model's lack of privilege is the security claim; policing a privilege it should not have is strictly worse than not granting it.

## References

- [ADR-013](ADR-013-langgraph-single-framework.md) (the framework choice whose rationale cited middleware) · [ADR-018](ADR-018-agentcore-policy.md) (the Gateway is authoritative) · [ADR-024](ADR-024-cedar-expresses-identity-and-shape.md) (what Cedar can actually express)
- CLAUDE.md invariant 0 · `tests/unit/test_import_boundary.py`
- ARCHITECTURE.md §9.2 (the policy set), §9.4 (enforcement mode)
