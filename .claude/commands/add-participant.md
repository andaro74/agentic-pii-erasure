---
description: Add a new participant AWS service implementing the 5-verb contract
---
Add a participant named $ARGUMENTS to src/pii_erasure/participants/.

A participant is one **Lambda function over one real AWS service**, registered as an AgentCore Gateway target (ADR-017). There is no local mode and no simulated participant — if you find yourself writing a fake, stop.

Process (README "Contributing a participant" + ADR-004, ADR-017):
1. Name the archetype it teaches and the AWS service that genuinely behaves that way. Model it on the closest existing participant; inherit from `participants/_base` — if you're copying verb plumbing, extend the base instead.
2. Implement all five verbs against the real service API. Residual honesty is invariant 7: if this system cannot fully delete (retention rule, suppression list, WORM, snapshot window), return `PARTIAL` with a populated `residual` and say why — never a hopeful `APPLIED`. `notify_suppression` is the worked example.
3. `_base/guard.py` re-validates `manifestDigest` and `approvalToken` in-process. AgentCore Policy is the control; the participant is the backstop for a misconfigured Gateway target. Do not skip it because the Gateway already checked.
4. Persist applied idempotency keys in DynamoDB; a replayed key returns `ALREADY_APPLIED` and does not double-apply. Lambda retries, Scheduler at-least-once delivery, and checkpoint resume all replay calls.
5. Add the service and its Lambda to `infra/stacks/participants.py` with **its own execution role scoped to exactly that one service**, and register the Gateway target in `infra/stacks/gateway.py`. No VPC configuration.
6. Add seed data in `seeds/` and extend `evals/fixtures/generator.py` so the ground-truth map covers it **in the same pass that writes it** — never hand-write the map (ADR-020).
7. Register it in `contract/registry.py`. Do NOT write bespoke conformance tests — the suite is parameterised over the registry and must pick it up automatically.
8. Unit-test the handler logic with `moto` so the fast loop stays fast, but remember moto is not a gate: the behaviours that matter (delete markers, index lag, lock windows) are the ones it does not model.

Done means two gates:
- **Hermetic (yours):** `make check` green, including the new synth assertions for the participant's role.
- **Deployed (the human's):** `make conformance` green including the new participant, and `make eval` still at recall 1.0.

State clearly which gate you ran and which you handed over.
