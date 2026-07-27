// ASDP Cedar policy set — the DEPLOYED artifact, not a description of one (ADR-018).
//
// One statement per file: AgentCore's CreatePolicy takes a single Cedar statement, so
// the file boundary IS the policy boundary and every deployed policy is reviewable on
// its own. Actions are enumerated per participant because AgentCore models each MCP
// tool as its own Cedar action — there is no context.toolName to pattern-match, and
// the only context that exists is context.input (ADR-024). The enumerations are
// asserted against the registry in tests/unit/test_policies.py, so participant #9
// cannot be missed quietly.
//
// The resource is one SPECIFIC gateway, whose ARN is substituted at deploy from
// the stack. Not style: CreatePolicy rejects a constrained action list scoped to
// `resource is AgentCore::Gateway` (any gateway) with a ValidationException, so a
// tool-specific policy must name the gateway it governs (V10-3).

// ── 4. No irreversible deletion without an approval token. Forbid wins. ──────
// Unconditional: it names no principal, so it binds the executor, a future service,
// and anything that comes to hold credentials. Cedar's forbid-wins semantics mean no
// permit added later can override it (§9.1).
//
// `has` guards precede every value read. An optional attribute read without one is a
// Cedar VALIDATION error, so the guards keep this policy deployable whichever way
// AgentCore maps the manifest's required fields — and a policy that fails validation
// never deploys, which is the good failure.
forbid(
  principal,
  action in [
    AgentCore::Action::"analytics-lake___hard_delete",
    AgentCore::Action::"billing-ledger___hard_delete",
    AgentCore::Action::"cognito-identity___hard_delete",
    AgentCore::Action::"compliance-archive___hard_delete",
    AgentCore::Action::"notify-suppression___hard_delete",
    AgentCore::Action::"profile-store___hard_delete",
    AgentCore::Action::"upload-bucket___hard_delete",
    AgentCore::Action::"vector-index___hard_delete"
  ],
  resource == AgentCore::Gateway::"{gateway_arn}"
)
unless {
  context.input has approvalToken &&
  context.input.approvalToken != "" &&
  context.input has manifestDigest &&
  context.input.manifestDigest like "sha256:*"
};
