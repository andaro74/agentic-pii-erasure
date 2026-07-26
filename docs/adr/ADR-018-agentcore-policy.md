# ADR-018: AgentCore Policy is the Cedar runtime

- **Status:** Accepted (refines [ADR-005](ADR-005-cedar-at-gateway.md), which remains accepted — this ADR names the implementation, not a different decision)
- **Anchors invariants:** CLAUDE.md #1 (discovery never gets a mutating tool), #3 (approval binds to digest), #6 (`restore` unreachable from phase 3)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-005](ADR-005-cedar-at-gateway.md) decided that authorization is enforced **at the tool boundary, outside the model's reasoning**, in Cedar, deny-by-default with forbid-wins semantics. It named "AgentCore Gateway" as the enforcement point at a time when the concrete product surface was not settled, and it hedged with a local engine implementing "a declared subset" so the offline demo could run.

Two things resolved since. **Policy in Amazon Bedrock AgentCore** is generally available and evaluates Cedar policies inside the Gateway on every tool invocation. And the offline demo is gone ([ADR-017](ADR-017-real-aws-participants.md)), so the local engine no longer needs to be an enforcement path — only a fast pre-check and a test surface.

## Decision

**AgentCore Policy, attached to the AgentCore Gateway, is the authoritative enforcement point.** `policies/cedar/*.cedar` is the deployed artifact, not a description of one.

Three IAM actions define the integration and each maps to a control:

| Action | Role |
|---|---|
| `bedrock-agentcore:AuthorizeAction` | Evaluates the policy set for one tool invocation. **This is the enforcement point.** |
| `bedrock-agentcore:PartiallyAuthorizeActions` | Returns the subset of tools a caller may invoke. The Gateway uses it to filter the MCP `tools/list` response per identity. |
| `bedrock-agentcore:GetPolicyEngine` | Retrieves the policy engine configuration (used by the deploy-time schema check below). |

`PartiallyAuthorizeActions` is the part worth dwelling on. **The discovery agent does not merely get denied when it calls `hard_delete` — it never sees that the tool exists.** Invariant #1 stops being a code-review rule about how the subgraph is constructed and becomes a property of the tool surface the model is handed. The `tool_surface_minimality` evaluator asserts it directly: `tools/list` for `asdp-discovery` returns exactly `subject.discover` and `subject.verify`.

Workload identities come from **AgentCore Identity**: `asdp-discovery`, `asdp-saga-executor`, `asdp-approval-service`. They are the Cedar principals, and they are distinct from the IAM execution roles that back them — both layers are required (ARCHITECTURE §9.3).

**Two backends, one rule set.** `policy/engine.py` evaluates the same declared rules in-process as a LangChain middleware pre-check, so a violation is caught before a Gateway round-trip and the decision is logged locally for the adversarial eval. A divergence test asserts the engine and the `.cedar` files agree. The middleware is a fast path and a test surface — **it is not the control**, because in-process enforcement is bypassable by any caller that forgets it, which is the original reason ADR-005 pushed enforcement outward.

**Entity and context names are validated at deploy time, not assumed.** The Gateway generates a schema; the `.cedar` files are checked against it during `cdk deploy` and the deploy fails on mismatch. ARCHITECTURE §9.2 marks its policy listing "illustrative" for this reason — a policy that references a context key the Gateway does not inject is a policy that silently never fires, which is the [VALIDATION.md](../VALIDATION.md) defect class exactly: a control that cannot go red.

## Consequences

- **Positive — enforcement is managed and outside our code.** No Cedar engine to operate, no policy distribution problem, and every decision lands in CloudWatch with full context.
- **Positive — the injection demo gets stronger.** Previously: the model is persuaded, Cedar refuses. Now: the model is persuaded, and there is no tool in its surface to call — and if one is fabricated, Cedar refuses. Two controls, both outside the model.
- **Positive — `LOG_ONLY` → `ENFORCING` is a deploy, not a runtime flag.** The rollout discipline ADR-005 required is now visible in CloudTrail.
- **Cost 1 — coupling to a specific AWS service.** ADR-005 was portable in principle; this is not. Accepted deliberately: the platform is AWS-only.
- **Cost 2 — the schema is generated, so policy authoring has a deploy-time feedback loop.** Mitigated by the deploy-time validation gate above; the alternative (assume and hope) is how policies become decoration.
- **Cost 3 — two backends must not drift.** Unchanged from ADR-005, and still covered by the divergence test.

## Alternatives considered

- **In-process middleware only.** Rejected, as in ADR-005: bypassable by any caller that forgets it, and it lives inside the process the injection is trying to influence.
- **Self-hosted Cedar / Amazon Verified Permissions in front of the Gateway.** Rejected: an extra hop and an extra thing to operate, for the same policy language AgentCore Policy already evaluates in the right place. Would also forfeit `PartiallyAuthorizeActions` tool-list filtering, which is the strongest single control here.
- **IAM alone.** Rejected: IAM cannot express `approvalToken.manifestDigest == manifestDigest` or a per-tenant velocity ceiling. IAM governs *which service* a role may call; Cedar governs *under what conditions* a tool may run. Both are used (§9.3); neither substitutes for the other.

## References

- ARCHITECTURE.md §9.1 (why Policy is the real control), §9.2 (policy set), §9.3 (IAM matrix), §9.4 (rollout), §11.4 (adversarial pass criterion)
- Refines [ADR-005](ADR-005-cedar-at-gateway.md) · [ADR-006](ADR-006-approval-binds-to-digest.md), [ADR-015](ADR-015-serverless-compute-split.md)
