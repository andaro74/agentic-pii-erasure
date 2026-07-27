// ASDP Cedar policy set — the DEPLOYED artifact, not a description of one (ADR-018).
//
// One statement per file: AgentCore's CreatePolicy takes a single Cedar statement, so
// the file boundary IS the policy boundary and every deployed policy is reviewable on
// its own. Actions are enumerated per participant because AgentCore models each MCP
// tool as its own Cedar action — there is no context.toolName to pattern-match, and
// the only context that exists is context.input (ADR-024). The enumerations are
// asserted against the registry in tests/unit/test_policies.py, so participant #9
// cannot be missed quietly.

// ── 3. Mutations require a well-formed manifest digest. ──────────────────────
// Cedar checks the SHAPE of the binding. It cannot check the BINDING: confirming a
// digest belongs to a signed, approved manifest needs a KMS signature verification,
// which no policy engine performs. ADR-024 records that split and names where the
// rest is enforced — the saga's hard_delete node and the participant precheck.
permit(
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
  resource is AgentCore::Gateway
)
when {
  principal.id like "*:assumed-role/asdp-{stage}-saga-executor" &&
  context.input has manifestDigest &&
  context.input.manifestDigest like "sha256:*"
};
