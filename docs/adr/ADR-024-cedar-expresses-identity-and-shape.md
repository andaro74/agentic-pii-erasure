# ADR-024: Cedar expresses identity and request shape — not business state

- **Status:** Accepted (supersedes the *policy set* in ARCHITECTURE §9.2; refines [ADR-018](ADR-018-agentcore-policy.md), which stands — the enforcement point is unchanged)
- **Anchors invariants:** CLAUDE.md #1 (discovery never gets a mutating tool), #3 (approval binds to the digest)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

[ADR-005](ADR-005-cedar-at-gateway.md) put authorization at the tool boundary; [ADR-018](ADR-018-agentcore-policy.md) named AgentCore Policy as the runtime and made schema validation a deploy-time gate rather than an assumption. Both stand.

What neither could state, because the product surface was not settled when they were written, is **what the generated Cedar schema actually contains**. ARCHITECTURE §9.2 hedged honestly — "entity type names below are illustrative" — but the policies it lists assume far more than naming: they read `context.subjectCount`, `context.legalHoldCount`, `context.approvalTokenValid`, `context.graceWindowElapsed`, `context.tenantDeletionsLast24h`, and `context.toolName`.

Verified against the AgentCore developer guide and the installed service model rather than recalled, the real schema is:

| Element | Reality |
|---|---|
| Principal | `AgentCore::OAuthUser` for a JWT gateway; **`AgentCore::IamEntity`** for an `AWS_IAM` gateway, with `.id` carrying `arn:aws:sts::<account>:assumed-role/<role-name>` |
| Resource | `AgentCore::Gateway` |
| Action | **One Cedar action per MCP tool** — `AgentCore::Action::"profile-store___hard_delete"`. There is no `context.toolName`. |
| Context | **`context.input` only** — the tool's declared input parameters, typed from the JSON Schema. Explicitly: "Cannot access context fields other than `context.input`." |

So six of §9.2's seven policies cannot be written as published. Not "are awkward to write" — **cannot be expressed at all**, because the facts they test are not in the request and Cedar cannot reach outside it.

The failure mode this creates is the one this repo keeps finding: a policy referencing `context.legalHoldCount` does not error. It validates against nothing, deploys clean, and silently never fires. A control that cannot go red.

## Decision

**Cedar enforces what is visible in the request: who is calling, which tool, and the shape of the arguments. Everything else is enforced where the fact actually lives, and this ADR names where.**

The deployed policy set (`policies/cedar/`, one Cedar statement per file):

| # | Policy | Enforces |
|---|---|---|
| 1 | discovery reads only | Invariant 1. Also drives `tools/list` filtering, so the identity is never *offered* a mutating tool |
| 2 | executor reads | Phase-3 `hold_recheck` and the verify/sweep nodes |
| 3 | executor mutates, with a well-formed `manifestDigest` | The *shape* of the digest binding |
| 4 | **forbid** any `hard_delete` without a non-empty `approvalToken` and a `sha256:` digest | The narrowest control; names no principal, so it binds everything |
| 5 | **forbid** discovery from mutating | Defence in depth behind policy 1 |

And what Cedar **cannot** enforce, with its actual home:

| §9.2 intent | Why Cedar cannot | Enforced instead by |
|---|---|---|
| `approvalTokenValid`, `approvedManifestDigest == manifestDigest` | Verifying a token means checking a **KMS signature**. No policy engine does cryptography. | `saga/nodes/hard_delete.py` re-verifies the token and re-digests the stored manifest on every entry, before any participant is called (V9-3's neighbourhood); the participant `_precheck` is the backstop |
| `legalHoldCount == 0` | Not in the request. Holds live in the participants. | `nodes/hold_check.py` at plan time and `nodes/hold_recheck.py` **live at phase-3 entry** (§5.3) |
| `graceWindowElapsed` | Not in the request; it is a property of the graph's position | Graph topology — `hard_delete` is unreachable except through `grace_window` |
| `subjectCount == 1` / blast-radius cap | Not in the request — **and unrepresentable**: every verb takes exactly one `subjectRef`. Bulk deletion has no wire form. | The contract's shape (`contract/verbs.py`), which is stronger than a policy: there is nothing to deny |
| `tenantDeletionsLast24h > 50` | Requires cross-request state Cedar has no access to | **Deferred, and open.** Recorded in ARCHITECTURE §16 rather than silently dropped |

**There is no second rule engine.** ADR-005 and ADR-018 both provided for an in-process engine evaluating "a declared subset", with a divergence test to keep the two honest. That subset is gone: `policy/engine.py` evaluates **the same `.cedar` files**, through Cedar (`cedarpy` wraps the same Rust implementation), against a schema reconstructed from the same tool manifest. Drift is removed rather than policed — a divergence test is a control you must maintain forever, and the thing it protects is a duplicate you chose to keep.

**Schema validation happens twice.** `CfnPolicy` deploys with `validationMode = FAIL_ON_ANY_FINDINGS`, so AWS validates each statement against the schema *it* generated and the deploy fails on mismatch — authoritative. `make policy-test` runs the same validation hermetically against a reconstruction built from `contract/tools.py`, the same manifest the Gateway targets publish. Fast gate, slow gate, one source of truth.

**`LOG_ONLY → ENFORCE` is a CloudFormation parameter** on the Gateway (the real enum is `ENFORCE`, not "ENFORCING"), so flipping it is a deploy and appears in CloudTrail (§9.4). Per-policy `enforcementMode` stays `ACTIVE`: one switch, in one place, or nobody can answer "is policy on?" from a single reading.

## Consequences

- **Positive — the policies that remain are real.** Every one is validated against the generated schema by the service itself, and every one is exercised hermetically with a red-proving mutation.
- **Positive — the strongest control survived intact.** Tool-list filtering by identity is exactly what §9.1 claimed, and it is the one that defeats prompt injection: the discovery agent is not denied `hard_delete`, it is never shown it.
- **Positive — the honest map got better.** "Cedar enforces everything" was never true. Naming where each rule actually lives makes the security story auditable instead of aspirational.
- **Cost 1 — the digest *binding* is not enforced at the Gateway.** Cedar sees a well-formed digest, not a valid one. The saga and the participant both re-check it, so the control exists twice in-band — but it is in-band, and ADR-005's argument against in-process enforcement applies to it. This is the most significant thing this ADR gives up, and it is stated plainly rather than buried.
- **Cost 2 — the velocity ceiling is unimplemented.** T3's containment for a compromised executor currently rests on the blast-radius cap being structural. Open question, not a solved one.
- **Cost 3 — role names are now load-bearing.** Cedar matches `principal.id like "*:assumed-role/asdp-<stage>-saga-executor"`, so the saga roles carry explicit names; a CDK-generated name would change on replacement and silently unbind the policy.

## Alternatives considered

- **Inject the missing facts as tool parameters** so Cedar can read them (`legalHoldCount` as an input field). Rejected: the caller would be asserting the facts it is being judged on. An executor that can claim `legalHoldCount = 0` is not constrained by a policy that checks it.
- **Keep §9.2's policies as written and accept they never fire.** Rejected — that is the definition of decoration, and [VALIDATION.md](../VALIDATION.md) exists because this repo keeps catching exactly this.
- **Switch the Gateway to `CUSTOM_JWT`** for `AgentCore::OAuthUser` principals with tags. Rejected: it would mean a Cognito pool for machine-to-machine callers that already have SigV4, for no gain in what Cedar can express. The IAM ARN is a perfectly good principal.

## References

- ARCHITECTURE §9.1–§9.4 · [ADR-005](ADR-005-cedar-at-gateway.md), [ADR-018](ADR-018-agentcore-policy.md) (both refined, neither reversed) · [ADR-006](ADR-006-approval-binds-to-digest.md)
- AgentCore developer guide: "Schema constraints", "Example policies" — the schema facts above are transcribed from those pages
