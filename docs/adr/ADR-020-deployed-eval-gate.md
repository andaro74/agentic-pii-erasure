# ADR-020: The recall gate runs against a deployed ephemeral stack

- **Status:** Accepted (refines [ADR-008](ADR-008-recall-1.0-hard-gate.md), which remains accepted; replaces the hermetic-CI mechanism [ADR-012](ADR-012-simulated-participants.md) provided)
- **Anchors invariants:** CLAUDE.md #8 (recall gates the build)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-008](ADR-008-recall-1.0-hard-gate.md) makes recall 1.0 a hard merge gate, and [ADR-012](ADR-012-simulated-participants.md) made that gate hermetic by running it against local fictional subsystems. ADR-012's strongest argument was explicit: *a merge gate must not depend on a cloud service being reachable.*

[ADR-017](ADR-017-real-aws-participants.md) removed the local subsystems. That leaves an unavoidable question — where does the gate run now? — and one tempting wrong answer: mock the participants for the eval and keep CI hermetic. That answer is wrong because it grades the agent against a fixture whose behaviour we authored, which is a short walk from grading it against a copy of its own output. [VALIDATION.md](../VALIDATION.md) baseline finding #4 is exactly that defect: *a fixture that couldn't fail*.

## Decision

**The recall gate runs against a deployed, ephemeral AWS stack, created and destroyed inside one CI workflow.**

```
cdk deploy (eval stack)  →  seed + emit ground-truth map  →  run discovery blind
        →  compute recall/precision  →  assert recall == 1.0  →  cdk destroy
```

Three properties are preserved, and they are the ones that made the gate trustworthy in the first place:

1. **Ground truth is generated, not labelled.** `evals/fixtures/generator.py` writes synthetic subjects into the real services and emits the placement map **in the same pass**. Discovery runs blind. The answer key cannot drift from the data because it is a by-product of writing the data.
2. **Recall 1.0 is a hard fail.** Unchanged. When the gate goes red the fix is a better discovery agent or a new fixture — never a lowered threshold (invariant #8).
3. **The stack is fresh per run.** No accumulated state, no cross-run contamination, no "it passed because yesterday's data was still there."

**What stays hermetic** and remains `make check` on every commit, with no AWS account: unit tests, canonicalisation and digest stability, reducer concurrency tests, the policy engine and its Cedar divergence test, and `cdk synth` with its IAM assertions. The fast loop stays fast; only the gates that genuinely need real service semantics need an account.

**AgentCore Evaluations** runs the same assertions continuously against the long-lived dev stack, using its built-in trajectory and tool-use evaluators alongside the custom recall evaluator. It is the **drift monitor**, not the gate — `evals/run.py` remains the gate of record, because a merge gate should fail for a reason you can read in the diff.

## Consequences

- **Positive — the gate grades against reality.** A recall failure caused by GSI lag, an orphaned embedding in `vector-index`, or an Iceberg snapshot is now catchable. Under a mock, none of those exist.
- **Positive — the fixture still cannot be tautological.** The one property that made the old gate meaningful is the one that transferred.
- **Cost 1 — the merge gate now depends on AWS being reachable, and ADR-012 was right that this is bad.** A regional outage or a credentials problem blocks merges. Mitigations: the hermetic `make check` suite still runs and still catches most regressions; the eval workflow is retried once; and a documented break-glass path lets a maintainer merge with the deployed gates skipped, which is recorded in the PR and re-run on the next green build. **Break-glass is a logged exception, never a default.**
- **Cost 2 — money and time per PR.** A stack create/seed/eval/destroy cycle is minutes and real spend. The eval stack omits nothing (a partial stack would grade a partial agent), so the cost is what it is; teardown is enforced by a workflow `always()` step so a failed run cannot leak resources. **[ADR-021](ADR-021-s3-vectors-for-cost.md) materially reduced this cost** by removing the last continuously-billing component, so per-run spend now scales with work done rather than with how long the stack existed — which makes this gate easier to defend against the standing pressure to mock it.
- **Cost 3 — flakiness has a new source.** Eventual consistency is real now. The eval harness waits on explicit consistency signals rather than sleeping, and a consistency-related recall failure is a fixture bug to fix, not a threshold to relax.

## Alternatives considered

- **Mock the participants for the eval, keep CI hermetic.** Rejected. It restores ADR-012's convenience while destroying the property that made the gate worth having — the agent would be graded against behaviour we wrote for it.
- **A long-lived shared eval stack.** Rejected: accumulated state across runs makes "did it pass because the agent is right, or because yesterday's tombstones are still there?" unanswerable.
- **Run the gate nightly instead of per PR.** Rejected: a merge gate that runs after the merge is not a gate. Nightly chaos runs exist separately and serve a different purpose.

## References

- ARCHITECTURE.md §11.2 (ground truth by construction), §11.5 (testing pyramid), §14.1 (no local mode)
- Refines [ADR-008](ADR-008-recall-1.0-hard-gate.md) · [ADR-017](ADR-017-real-aws-participants.md) · [VALIDATION.md](../VALIDATION.md) baseline finding #4
