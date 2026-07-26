---
description: Work the next unchecked milestone from docs/ROADMAP.md end to end
---
Open docs/ROADMAP.md and take the first unchecked milestone (or the one I name: $ARGUMENTS).

1. Restate the milestone's goal and **both** of its "done when" gates before writing anything — the hermetic one and the deployed one.
2. Re-read the CLAUDE.md invariants listed under the milestone's Traps.
3. For any code touching langgraph/langchain: verify the API against the *installed pinned version first* — read the installed package source or `pip show -f` it. For any AWS or AgentCore code: verify the API shape against the service's current documentation. Remembered signatures are not evidence, and AgentCore is young enough that this matters (ADR-016, ADR-018).
4. Implement in small steps, running tests as you go. Nodes under `saga/nodes/` never construct a model client, and the `saga-executor` role never gains a `bedrock:*` action. Anything you cannot make work must fail loudly — no stub that pretends success, and no mock standing in for a real service.
5. Finish the **hermetic** gate: it and `make check` must both pass. Paste their real output.
6. The **deployed** gate is mine to run — `make deploy-dev` and everything downstream of it spends money and is denied to you. Tell me exactly which commands to run and what output would count as passing.
7. Tick the milestone checkbox only once I confirm the deployed gate passed. If reality diverged from ARCHITECTURE.md or PROJECT-STRUCTURE.md, fix the doc in the same commit or write a superseding ADR — never let them drift silently.
8. End by naming anything you deferred and which gate you did not run, so the next session inherits an honest state.
