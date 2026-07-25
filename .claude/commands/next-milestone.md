---
description: Work the next unchecked milestone from docs/ROADMAP.md end to end
---
Open docs/ROADMAP.md and take the first unchecked milestone (or the one I name: $ARGUMENTS).

1. Restate the milestone's goal and its exact "done when" command before writing anything.
2. Re-read the CLAUDE.md invariants listed under the milestone's Traps.
3. For any code touching langgraph/langchain: verify the API against the *installed pinned version first* — read the installed package source or `pip show -f` it. Remembered signatures are not evidence (ADR-014).
4. Implement in small steps, running tests as you go. Nodes under saga/nodes/ never construct a model client. Anything you cannot make work must fail loudly — no stub that pretends success.
5. Finish only when the milestone's "done when" command AND `make check` both pass. Paste their real output.
6. Tick the milestone checkbox in docs/ROADMAP.md in the same commit. If reality diverged from ARCHITECTURE.md or PROJECT-STRUCTURE.md, fix the doc in the same commit or write a superseding ADR — never let them drift silently.
7. End by naming anything you deferred, so the next session inherits an honest state.
