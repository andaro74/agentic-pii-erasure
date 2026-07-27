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

// ── 5. The discovery agent may NEVER mutate. Defence in depth. ───────────────
// Redundant against policy 1 by construction — nothing permits it, and Cedar denies
// by default. Keep it anyway: its job is to survive a future permit that widens
// discovery's scope by accident, and forbid-wins guarantees it holds regardless of
// what anyone adds later. ARCHITECTURE §9.2 makes the same argument for its policy 7.
forbid(
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"analytics-lake___hard_delete",
    AgentCore::Action::"analytics-lake___restore",
    AgentCore::Action::"analytics-lake___soft_delete",
    AgentCore::Action::"billing-ledger___hard_delete",
    AgentCore::Action::"billing-ledger___restore",
    AgentCore::Action::"billing-ledger___soft_delete",
    AgentCore::Action::"cognito-identity___hard_delete",
    AgentCore::Action::"cognito-identity___restore",
    AgentCore::Action::"cognito-identity___soft_delete",
    AgentCore::Action::"compliance-archive___hard_delete",
    AgentCore::Action::"compliance-archive___restore",
    AgentCore::Action::"compliance-archive___soft_delete",
    AgentCore::Action::"notify-suppression___hard_delete",
    AgentCore::Action::"notify-suppression___restore",
    AgentCore::Action::"notify-suppression___soft_delete",
    AgentCore::Action::"profile-store___hard_delete",
    AgentCore::Action::"profile-store___restore",
    AgentCore::Action::"profile-store___soft_delete",
    AgentCore::Action::"upload-bucket___hard_delete",
    AgentCore::Action::"upload-bucket___restore",
    AgentCore::Action::"upload-bucket___soft_delete",
    AgentCore::Action::"vector-index___hard_delete",
    AgentCore::Action::"vector-index___restore",
    AgentCore::Action::"vector-index___soft_delete"
  ],
  resource == AgentCore::Gateway::"{gateway_arn}"
)
when { principal.id like "*:assumed-role/asdp-{stage}-discovery" };
