"""The published MCP tool surface — the five verbs as the Gateway advertises them.

This lives in `contract/` and not in the CDK stack because **three things must agree
and none of them may drift**: what the Gateway publishes as targets, what the Cedar
policies name as actions, and what the Cedar *schema* says each action's input looks
like. AgentCore generates that schema from the tool manifest, so a policy referencing
a field the manifest does not declare validates against nothing and silently never
fires (ADR-018). One definition, three consumers, no second copy to rot.

Wire names are target-prefixed by the service — `upload-bucket___hard_delete` — and
the delimiter is the same `___` the participant harness strips. A Cedar *action* is
exactly that wire name: `AgentCore::Action::"upload-bucket___hard_delete"`.

No boto3, no framework: `contract/` stays liftable (invariant 0).
"""

from __future__ import annotations

from typing import Any

from pii_erasure.contract.registry import system_ids

TOOL_NAME_DELIMITER = "___"

_STRING = {"type": "string"}


def _artifact_array() -> dict[str, Any]:
    return {
        "type": "array",
        "description": "Echo of the approved artifact set",
        "items": {
            "type": "object",
            "properties": {
                "kind": _STRING,
                "locator": _STRING,
                "count": {"type": "integer"},
                "classification": {"type": "array", "items": _STRING},
                "retentionUntil": _STRING,
            },
            "required": ["kind", "locator"],
        },
    }


def _mutation_schema(*, approval: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "subjectRef": {"type": "string", "description": "Pseudonymous handle, never raw PII"},
        "sagaId": _STRING,
        "manifestDigest": {"type": "string", "description": "Binds this call to an approved plan"},
        "idempotencyKey": _STRING,
        "artifacts": _artifact_array(),
        "dryRun": {"type": "boolean"},
    }
    required = ["subjectRef", "sagaId", "manifestDigest", "idempotencyKey", "artifacts"]
    if approval:
        properties["approvalToken"] = {
            "type": "string",
            "description": "Digest-bound approval token (ADR-006)",
        }
        required.append("approvalToken")
    return {"type": "object", "properties": properties, "required": required}


def _read_schema(*, hints: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {"subjectRef": _STRING, "sagaId": _STRING}
    if hints:
        properties["scopeHints"] = {"type": "array", "items": _STRING}
    return {"type": "object", "properties": properties, "required": ["subjectRef", "sagaId"]}


#: The five verbs, as the Gateway publishes them. Order is fixed so the synthesised
#: template is stable; the read-only pair is first because that is the subset a
#: discovery identity is ever permitted to see (invariant 1).
TOOL_DEFINITIONS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("discover", "Read-only. What exists for this subject here?", _read_schema(hints=True)),
    ("verify", "Read-only assertion. Must return zero artifacts.", _read_schema(hints=False)),
    (
        "soft_delete",
        "Reversible. Disable, tombstone, or mark pending-anonymization.",
        _mutation_schema(approval=False),
    ),
    (
        "restore",
        "The compensating transaction for soft_delete. Never reachable from phase 3.",
        _mutation_schema(approval=False),
    ),
    (
        "hard_delete",
        "Irreversible. Purge or crypto-shred. Requires a digest-bound approval token.",
        _mutation_schema(approval=True),
    ),
)

#: Bare tool names, in publication order.
TOOL_NAMES: tuple[str, ...] = tuple(name for name, _description, _schema in TOOL_DEFINITIONS)

#: The read-only pair. Invariant 1's allowlist, expressed on the wire surface.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"discover", "verify"})

#: Everything that changes a participant's state.
MUTATING_TOOLS: frozenset[str] = frozenset(TOOL_NAMES) - READ_ONLY_TOOLS


def action_name(system_id: str, tool: str) -> str:
    """The Cedar action / MCP tool name for one (participant, verb) pair."""
    return f"{system_id}{TOOL_NAME_DELIMITER}{tool}"


def action_names(tools: frozenset[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Every action for the given verbs, across every registered participant.

    Sorted so the generated policy text and Cedar schema are byte-stable: a policy
    file that reorders itself between runs produces a spurious deploy diff, and a
    reviewer learns to ignore diffs that always appear.
    """
    return tuple(
        sorted(action_name(system_id, tool) for system_id in system_ids() for tool in tools)
    )
