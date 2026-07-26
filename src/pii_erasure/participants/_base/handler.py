"""The participant Lambda harness: one dispatch path, five verbs, no per-participant plumbing.

Inherit from `Participant` and implement the verbs. Everything below — tool-name parsing,
request validation, the refusal pre-checks, idempotency, evidence, error shaping — is
shared, so adding participant #9 is a handler and a Gateway target, not a re-derivation
of the contract.

**How a call arrives.** AgentCore Gateway invokes a Lambda target with the tool's input
schema properties as the *event*, and the verb in
``context.client_context.custom['bedrockAgentCoreToolName']``, formatted
``${target_name}___${tool_name}`` — verified against the AgentCore developer guide, not
recalled. The conformance suite drives this same path by passing `ClientContext` to
`lambda:Invoke`, deliberately: a test-only entry point would prove the test-only entry
point works.

**No framework here.** `langgraph` and `langchain` are not importable from this package
(invariant 0), and nothing in it constructs a model client. A participant executes; it
does not decide.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pii_erasure.contract import (
    Archetype,
    DiscoverRequest,
    DiscoverResponse,
    DiscoveryEvidence,
    HardDeleteRequest,
    MutationResponse,
    Outcome,
    ReceiptEvidence,
    RestoreRequest,
    SoftDeleteRequest,
    Verb,
    VerifyRequest,
    VerifyResponse,
    canonical,
)
from pii_erasure.participants._base.idempotency import IdempotencyLog

#: Gateway-visible tool names, which are namespaced by the target rather than by a
#: prefix in the name itself: the wire name is ``upload-bucket___discover``. The
#: contract's ``subject.discover`` stays the conceptual identifier used in Cedar and the
#: docs; a dot is not a safe character in an MCP tool name.
TOOL_NAMES: dict[str, Verb] = {
    "discover": Verb.DISCOVER,
    "soft_delete": Verb.SOFT_DELETE,
    "restore": Verb.RESTORE,
    "hard_delete": Verb.HARD_DELETE,
    "verify": Verb.VERIFY,
}

TOOL_NAME_DELIMITER = "___"

#: A digest is `sha256:` followed by 64 hex characters. Checked for *shape* here; checked
#: for *authenticity* by AgentCore Policy at the Gateway (M6) and against the signed
#: manifest (M3). This is the backstop, not the control.
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_MUTATING_TOOLS = frozenset({"soft_delete", "restore", "hard_delete"})


class ParticipantError(RuntimeError):
    """A call this participant cannot make sense of. Fails the invocation, loudly.

    Distinct from `REFUSED`, which is a *contract* answer meaning "I understood you and
    declined". A malformed call is a bug somewhere, and swallowing it as a refusal would
    make the bug indistinguishable from a policy decision.
    """


class Participant(ABC):
    """One AWS service, five verbs."""

    system_id: str
    archetype: Archetype

    #: Artifact kinds this participant can never remove. Declared per archetype so
    #: `discover` can report the truth at plan time rather than surprising phase 3.
    undeletable_kinds: frozenset[str] = frozenset()

    @abstractmethod
    def discover(self, request: DiscoverRequest) -> DiscoverResponse: ...

    @abstractmethod
    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse: ...

    @abstractmethod
    def restore(self, request: RestoreRequest) -> MutationResponse: ...

    @abstractmethod
    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse: ...

    @abstractmethod
    def verify(self, request: VerifyRequest) -> VerifyResponse: ...


def tool_name_from_context(client_context: Any) -> str:
    """Extract the bare verb from the Gateway's target-prefixed tool name."""
    custom = getattr(client_context, "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName")
    if not raw:
        raise ParticipantError(
            "no bedrockAgentCoreToolName in the client context — a participant only ever "
            "acts on a named verb, never on an inferred one"
        )
    name = str(raw)
    _, _, bare = name.partition(TOOL_NAME_DELIMITER)
    return bare or name


def discovery_evidence(query: dict[str, Any]) -> DiscoveryEvidence:
    """Evidence for a read. `queryDigest` lets an auditor confirm *what was asked*."""
    return DiscoveryEvidence(
        query_digest=f"sha256:{hashlib.sha256(canonical(query)).hexdigest()}",
        observed_at=_now(),
    )


def receipt_evidence(receipt: dict[str, Any]) -> ReceiptEvidence:
    """Evidence for a write. `receiptDigest` covers what was actually actioned."""
    return ReceiptEvidence(
        receipt_digest=f"sha256:{hashlib.sha256(canonical(receipt)).hexdigest()}",
        applied_at=_now(),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _refusal(system_id: str, reason: str) -> dict[str, Any]:
    """A contract-shaped refusal. Nothing was changed; `affected` is zero and says so."""
    return MutationResponse(
        system_id=system_id,
        outcome=Outcome.REFUSED,
        affected=0,
        evidence=receipt_evidence({"refused": reason}),
    ).digested_body()


def _precheck(tool: str, event: dict[str, Any], system_id: str) -> dict[str, Any] | None:
    """Refuse a mutation that arrives without its binding, before anything is parsed.

    Conformance asserts that `hard_delete` refuses when the digest or token is absent —
    so this is a *contract answer*, not an exception. The digest is checked for presence
    and shape only; binding it to an approved manifest needs the manifest (M3) and the
    Cedar check at the Gateway (M6), and claiming more than that here would be the kind
    of untested claim docs/VALIDATION.md exists to catch.
    """
    if tool not in _MUTATING_TOOLS:
        return None

    digest = event.get("manifestDigest")
    if not digest or not _DIGEST_PATTERN.match(str(digest)):
        return _refusal(system_id, "absent or malformed manifestDigest")
    if tool == "hard_delete":
        token = event.get("approvalToken")
        if not token:
            return _refusal(system_id, "hard_delete without an approval token")
    return None


def dispatch(
    participant: Participant,
    event: dict[str, Any],
    context: Any,
    *,
    idempotency: IdempotencyLog | None = None,
) -> dict[str, Any]:
    """Route one Gateway invocation to one verb and return the contract response body."""
    tool = tool_name_from_context(getattr(context, "client_context", None))
    if tool not in TOOL_NAMES:
        raise ParticipantError(f"unknown tool {tool!r} for {participant.system_id}")

    refusal = _precheck(tool, event, participant.system_id)
    if refusal is not None:
        return refusal

    if tool == "discover":
        return participant.discover(DiscoverRequest.model_validate(event)).digested_body()
    if tool == "verify":
        return participant.verify(VerifyRequest.model_validate(event)).digested_body()

    return _mutate(participant, tool, event, idempotency)


def _mutate(
    participant: Participant,
    tool: str,
    event: dict[str, Any],
    idempotency: IdempotencyLog | None,
) -> dict[str, Any]:
    """Run a mutating verb exactly once per idempotency key."""
    request: SoftDeleteRequest | RestoreRequest | HardDeleteRequest
    if tool == "soft_delete":
        request = SoftDeleteRequest.model_validate(event)
    elif tool == "restore":
        request = RestoreRequest.model_validate(event)
    else:
        request = HardDeleteRequest.model_validate(event)

    if request.dry_run:
        # A dry run reports what *would* happen and touches nothing — so it must not
        # burn the idempotency key, or the real call would replay as ALREADY_APPLIED.
        return MutationResponse(
            system_id=participant.system_id,
            outcome=Outcome.REFUSED,
            affected=0,
            evidence=receipt_evidence({"dryRun": True, "artifacts": len(request.artifacts)}),
        ).digested_body()

    if idempotency is None:
        return _apply(participant, tool, request).digested_body()

    prior = idempotency.claim(system_id=participant.system_id, key=request.idempotency_key)
    if prior is not None:
        # Return what the original call returned, relabelled. The affected count and the
        # residual stay truthful — the caller learns what happened, and that it was not
        # this invocation that made it happen.
        return {**prior, "outcome": Outcome.ALREADY_APPLIED.value}

    try:
        response = _apply(participant, tool, request)
    except Exception:
        idempotency.release(system_id=participant.system_id, key=request.idempotency_key)
        raise

    body = response.digested_body()
    idempotency.complete(
        system_id=participant.system_id, key=request.idempotency_key, response=body
    )
    return body


def _apply(
    participant: Participant,
    tool: str,
    request: SoftDeleteRequest | RestoreRequest | HardDeleteRequest,
) -> MutationResponse:
    if tool == "soft_delete":
        assert isinstance(request, SoftDeleteRequest)
        return participant.soft_delete(request)
    if tool == "restore":
        assert isinstance(request, RestoreRequest)
        return participant.restore(request)
    assert isinstance(request, HardDeleteRequest)
    return participant.hard_delete(request)
