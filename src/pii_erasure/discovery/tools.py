"""The MCP client over the AgentCore Gateway — and invariant 1's first enforcement point.

The discovery agent speaks one protocol to one endpoint. It never learns there are eight
backends, never holds a participant's SDK, and never holds an IAM permission on a
participant's service: its only route to subject data is a SigV4-signed MCP call to the
Gateway, where Cedar decides.

**A mutating verb cannot be constructed here.** :func:`read_only_toolset` refuses any verb
outside `contract.tools.READ_ONLY_TOOLS`, at construction, with the class of the offending
verb named. That is one of the three independent places invariant 1 lives; the other two
are the Cedar permit (`policies/cedar/01`, `05`) and Gateway tool-list filtering via
`PartiallyAuthorizeActions`, which means the model is never *offered* a mutating tool.
None of the three is redundant, and none may be weakened on the grounds that the other
two exist — this one is the only one that fails on a developer's laptop, before a deploy.

Transport note: the Gateway's streamable-HTTP transport may answer `application/json` or
`text/event-stream` for the same request. Both are parsed here rather than one being
assumed, because assuming produced a silent empty tool list the first time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from pii_erasure.contract.registry import system_ids
from pii_erasure.contract.tools import READ_ONLY_TOOLS, action_name

#: The Gateway is an AgentCore data-plane surface; SigV4 is signed against this service.
SIGNING_SERVICE = "bedrock-agentcore"

_JSON = "application/json"
_SSE = "text/event-stream"


class MutatingToolRefusedError(ValueError):
    """A mutating verb was requested for a discovery toolset (invariant 1).

    Raised at construction, not at call time. A discovery agent holding a
    `hard_delete` tool it merely chooses not to use is not the property this
    architecture claims — the claim is that the tool does not exist for it.
    """

    def __init__(self, verbs: Sequence[str]) -> None:
        offending = ", ".join(sorted(verbs))
        super().__init__(
            f"discovery may hold only {sorted(READ_ONLY_TOOLS)}; refused: [{offending}]. "
            "This is invariant 1 and it has no override, flag, or debug path."
        )
        self.verbs = tuple(sorted(verbs))


class GatewayError(RuntimeError):
    """The Gateway refused or failed a call. Carries the decision, never the payload."""


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation, recorded for the trajectory evaluators (§11.3).

    `denied` is what `no_premature_hard_delete` and the adversarial suite assert on:
    the pass criterion is *the tool was absent, or policy denied and logged* — never
    that the model declined. So the denial has to be observable, which means recorded.
    """

    tool: str
    system_id: str
    verb: str
    ok: bool
    denied: bool = False
    error: str | None = None


@dataclass
class GatewayToolset:
    """A read-only view of the Gateway, bound to one subject and saga.

    Construct via :func:`read_only_toolset`; the constructor is not the guard.
    """

    gateway_url: str
    region: str
    verbs: tuple[str, ...]
    session: Any = None
    timeout: float = 60.0
    #: Every call made through this toolset, in order. The trajectory record.
    calls: list[ToolCall] = field(default_factory=list)
    _rid: int = 0

    def __post_init__(self) -> None:
        # Belt and braces: `read_only_toolset` is the front door, but a caller that
        # builds the dataclass directly must not get a weaker object than one that
        # goes through it.
        _refuse_mutating(self.verbs)
        if self.session is None:
            self.session = boto3.Session()

    # ── transport ────────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._rid += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._rid, "method": method, "params": params}
        ).encode()
        signed = AWSRequest(
            method="POST",
            url=self.gateway_url,
            data=body,
            headers={"Content-Type": _JSON, "Accept": f"{_JSON}, {_SSE}"},
        )
        credentials = self.session.get_credentials()
        if credentials is None:
            raise GatewayError("no AWS credentials available to sign a Gateway call")
        SigV4Auth(credentials.get_frozen_credentials(), SIGNING_SERVICE, self.region).add_auth(
            signed
        )
        request = urllib.request.Request(  # noqa: S310 — URL is a stack output, not input
            self.gateway_url, data=body, headers=dict(signed.headers), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return _decode(response.read().decode(), response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as error:
            # A Cedar denial arrives as an HTTP error, and it is a RESULT, not a
            # transport failure — the adversarial gate's pass condition depends on
            # telling the two apart.
            return {"error": {"code": error.code, "message": error.read().decode(errors="replace")}}

    # ── the two verbs, and only the two ──────────────────────────────────────

    def list_tools(self) -> tuple[str, ...]:
        """Every tool the Gateway is willing to show this identity.

        The `tool_surface_minimality` evaluator asserts this equals exactly the read
        verbs across the registry — measured against the deployed Gateway, which is
        the only place `PartiallyAuthorizeActions` actually runs.
        """
        names: list[str] = []
        cursor: str | None = None
        for _page in range(20):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            envelope = self._rpc("tools/list", params)
            if "error" in envelope:
                raise GatewayError(f"tools/list failed: {envelope['error']}")
            result = envelope.get("result") or {}
            names.extend(tool["name"] for tool in result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tuple(names)

    def call(self, system_id: str, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one read verb on one participant. Records the call either way."""
        if verb not in self.verbs:
            raise MutatingToolRefusedError([verb])
        tool = action_name(system_id, verb)
        envelope = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if "error" in envelope:
            message = str(envelope["error"])
            denied = any(
                marker in message.lower()
                for marker in ("denied", "not authorized", "unauthorized", "forbid")
            )
            self.calls.append(
                ToolCall(tool, system_id, verb, ok=False, denied=denied, error=message[:400])
            )
            raise GatewayError(f"{tool}: {message[:400]}")
        self.calls.append(ToolCall(tool, system_id, verb, ok=True))
        return _tool_payload(envelope.get("result") or {})


def _refuse_mutating(verbs: Sequence[str]) -> tuple[str, ...]:
    offending = [verb for verb in verbs if verb not in READ_ONLY_TOOLS]
    if offending:
        raise MutatingToolRefusedError(offending)
    return tuple(verbs)


def read_only_toolset(
    *,
    gateway_url: str,
    region: str,
    verbs: Sequence[str] = ("discover", "verify"),
    session: Any = None,
) -> GatewayToolset:
    """Build the discovery toolset. Refuses anything that can change a participant."""
    return GatewayToolset(
        gateway_url=gateway_url,
        region=region,
        verbs=_refuse_mutating(verbs),
        session=session,
    )


def expected_tool_surface(verbs: Sequence[str] = ("discover", "verify")) -> tuple[str, ...]:
    """What `tools/list` must return for the discovery identity — registry-driven, so
    participant #9 is covered the moment it is registered rather than when someone
    remembers to extend a literal list."""
    _refuse_mutating(verbs)
    return tuple(sorted(action_name(s, verb) for s in system_ids() for verb in verbs))


def _decode(text: str, content_type: str) -> dict[str, Any]:
    if _SSE in content_type:
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else "{}"
    parsed: dict[str, Any] = json.loads(text or "{}")
    return parsed


def _tool_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap MCP's content envelope to the participant's own JSON response.

    MCP returns `content: [{type: "text", text: "<json>"}]`; `structuredContent` is
    preferred when present. An `isError: true` result is a participant-level failure
    and is surfaced rather than parsed into a shape that looks like success.
    """
    if result.get("isError"):
        raise GatewayError(f"participant returned an error result: {str(result)[:400]}")
    if isinstance(result.get("structuredContent"), dict):
        return dict(result["structuredContent"])
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise GatewayError("no JSON payload in the MCP result envelope")
