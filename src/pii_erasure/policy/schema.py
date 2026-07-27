"""The Cedar schema, derived from the tool manifest — never assumed (ADR-018).

AgentCore generates a Cedar schema from the Gateway's MCP tool manifest and validates
every policy against it. A policy naming a context key the manifest does not declare
is not a policy that fails loudly; it is a policy that **validates against nothing and
silently never fires** — decoration wearing the costume of a control, and the exact
defect class docs/VALIDATION.md exists to catch.

The authoritative check is the service's own: `CfnPolicy` is deployed with
`validationMode = FAIL_ON_ANY_FINDINGS`, so AWS refuses a policy that does not
validate against the schema it actually generated. But a deploy-time-only check has a
feedback loop measured in minutes and dollars, so this module reconstructs the same
schema **from the same `TOOL_DEFINITIONS` the Gateway stack publishes** and
`make policy-test` validates against it hermetically. Two gates, one source of truth,
and the fast one cannot drift from the deployed one because both read this manifest.

Reconstructed, not fetched — so the honest limit is stated plainly: if AgentCore
changes how it maps a JSON Schema to Cedar types, this file is wrong until someone
updates it, and the deployed `FAIL_ON_ANY_FINDINGS` gate is what catches that. The
mapping below is transcribed from the AgentCore developer guide's "Schema constraints"
page (string → String, integer → Long, boolean → Bool, number → Decimal), not recalled.
"""

from __future__ import annotations

import json
from typing import Any

from pii_erasure.contract.registry import system_ids
from pii_erasure.contract.tools import TOOL_DEFINITIONS, action_name

#: The Cedar namespace AgentCore generates. Policies may not define entity types
#: outside it, so this is a constant rather than a setting.
NAMESPACE = "AgentCore"

#: Principal type for an `AWS_IAM` Gateway. An OAuth gateway would generate
#: `AgentCore::OAuthUser` with tag support instead; ours is IAM by deliberate choice
#: (see the GatewayStack docstring), which makes the IAM role ARN the Cedar principal
#: and removes a whole identity-mapping layer.
PRINCIPAL_TYPE = "IamEntity"
RESOURCE_TYPE = "Gateway"

#: JSON Schema → Cedar type. Transcribed from the AgentCore developer guide.
_JSON_TO_CEDAR = {
    "string": "String",
    "integer": "Long",
    "boolean": "Bool",
    "number": "Decimal",
}


def _cedar_attribute(json_schema: dict[str, Any], *, required: bool) -> dict[str, Any] | None:
    """One tool-input property as a Cedar attribute, or None if unmappable.

    Arrays and objects are deliberately dropped rather than guessed at: no policy in
    this system reasons about the artifact array, and inventing a shape for it would
    put a claim in the schema that the real generated one might contradict.
    """
    cedar_type = _JSON_TO_CEDAR.get(str(json_schema.get("type")))
    if cedar_type is None:
        return None
    return {"type": cedar_type, "required": required}


def _action_context(input_schema: dict[str, Any]) -> dict[str, Any]:
    """`context.input` for one tool — the ONLY context AgentCore injects."""
    required = set(input_schema.get("required", ()))
    attributes: dict[str, Any] = {}
    for name, prop in input_schema.get("properties", {}).items():
        attribute = _cedar_attribute(prop, required=name in required)
        if attribute is not None:
            attributes[name] = attribute
    return {
        "type": "Record",
        "attributes": {
            "input": {
                "type": "Record",
                "attributes": attributes,
                "required": True,
            }
        },
    }


def cedar_schema() -> dict[str, Any]:
    """The schema every policy in `policies/cedar/` is validated against."""
    actions: dict[str, Any] = {}
    for system_id in system_ids():
        for tool, _description, input_schema in TOOL_DEFINITIONS:
            actions[action_name(system_id, tool)] = {
                "appliesTo": {
                    "principalTypes": [PRINCIPAL_TYPE],
                    "resourceTypes": [RESOURCE_TYPE],
                    "context": _action_context(input_schema),
                }
            }
    return {
        NAMESPACE: {
            "entityTypes": {
                # `.id` carries the caller's IAM ARN — for an assumed role it is
                # `arn:aws:sts::<account>:assumed-role/<role-name>`, stable across
                # sessions, which is what makes `principal.id like` usable.
                PRINCIPAL_TYPE: {
                    "shape": {
                        "type": "Record",
                        "attributes": {"id": {"type": "String", "required": True}},
                    }
                },
                RESOURCE_TYPE: {"shape": {"type": "Record", "attributes": {}}},
            },
            "actions": actions,
        }
    }


def cedar_schema_json() -> str:
    return json.dumps(cedar_schema(), sort_keys=True, indent=2)
