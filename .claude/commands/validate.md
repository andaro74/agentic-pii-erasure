---
description: Run a Fable-5-style validation pass over recent changes
---
Run the validation discipline from docs/VALIDATION.md over $ARGUMENTS (default: everything changed since the last validation entry).

The governing question for every claim, test, gate, and doc statement: **"would this actually execute?"** The last pass found four High-severity defects with one root cause — controls written before their mechanism describe the intention, not the mechanism. A test that can't pass, a gate that can't gate, a pin protecting the wrong layer.

Sweep:
1. Claims-vs-mechanism: for each assertion in docs or comments about what a test/gate/pin enforces, confirm the enforcing code exists and would fire.
2. Doc drift: grep for names, paths, and framework vocabulary that no longer match reality (superseded ADRs and VALIDATION.md are the only legitimate holders of stale vocabulary).
3. Structural: all relative markdown links resolve; embedded diagrams in ARCHITECTURE.md remain byte-identical to docs/diagrams/ sources; Mermaid blocks balance.
4. Executable: run `make check` and every "done when" of milestones marked complete in docs/ROADMAP.md.
5. Append a dated findings table (ID · severity · finding · resolution) to docs/VALIDATION.md — including "no findings" if true, dated. Fix what you find in the same session where feasible; otherwise record it honestly as open.
