---
description: Add a new participant subsystem implementing the 5-verb contract
---
Add a participant named $ARGUMENTS to src/pii_erasure/participants/.

Process (README "Contributing a participant" + ADR-004):
1. Model it on the closest existing archetype; inherit from participants/_base — if you're copying verb plumbing, extend the base instead.
2. Implement all five verbs. Residual honesty is invariant 7: if this system cannot fully delete (retention rule, suppression list, WORM), return PARTIAL with a populated `residual` and say why — never a hopeful APPLIED. pigeon_comms is the worked example.
3. Persist applied idempotency keys; a replayed key returns ALREADY_APPLIED and does not double-apply.
4. Add seed data in seeds/ and extend evals/fixtures/generator.py so the ground-truth map covers it in the same pass that writes it.
5. Register it. Do NOT write bespoke conformance tests — the suite is parameterised over the registry and must pick it up automatically.
6. Done means: `make conformance` green including the new participant, and `make eval` still at recall 1.0.
