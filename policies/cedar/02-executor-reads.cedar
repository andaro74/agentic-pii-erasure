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

// ── 2. The executor reads during phase 3. ────────────────────────────────────
// hold_recheck calls discover (holds are re-evaluated live, never cached from phase
// 1 — §5.3), and the verify and sweep nodes call verify.
permit(
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"analytics-lake___discover",
    AgentCore::Action::"analytics-lake___verify",
    AgentCore::Action::"billing-ledger___discover",
    AgentCore::Action::"billing-ledger___verify",
    AgentCore::Action::"cognito-identity___discover",
    AgentCore::Action::"cognito-identity___verify",
    AgentCore::Action::"compliance-archive___discover",
    AgentCore::Action::"compliance-archive___verify",
    AgentCore::Action::"notify-suppression___discover",
    AgentCore::Action::"notify-suppression___verify",
    AgentCore::Action::"profile-store___discover",
    AgentCore::Action::"profile-store___verify",
    AgentCore::Action::"upload-bucket___discover",
    AgentCore::Action::"upload-bucket___verify",
    AgentCore::Action::"vector-index___discover",
    AgentCore::Action::"vector-index___verify"
  ],
  resource == AgentCore::Gateway::"{gateway_arn}"
)
when { (principal.id like "*:assumed-role/asdp-{stage}-saga-executor"
        || principal.id like "*:assumed-role/asdp-{stage}-saga-executor/*") };
