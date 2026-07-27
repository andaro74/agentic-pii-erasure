"""Helpers shared by the executor nodes. As deterministic as the nodes themselves."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pii_erasure.contract import VerifyRequest, VerifyResponse
from pii_erasure.contract.registry import get as registry_get
from pii_erasure.manifest import Manifest

if TYPE_CHECKING:
    from pii_erasure.saga.deps import SagaDeps


class SagaStateError(RuntimeError):
    """The state does not carry what this node requires — a wiring bug, not a datum."""


def manifest_from_state(state: dict[str, Any]) -> Manifest:
    raw = state.get("manifest")
    if not raw:
        raise SagaStateError("no manifest in state — plan must run before execution nodes")
    return Manifest.model_validate(raw)


def digest_from_state(state: dict[str, Any]) -> str:
    digest = state.get("manifest_digest")
    if not digest:
        raise SagaStateError("no manifest digest in state — approval binding is impossible")
    return str(digest)


def receipt_key(verb: str, system_id: str) -> str:
    """The receipts-dict key. One shape everywhere, so replays can skip completed work."""
    return f"{verb}:{system_id}"


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def verify_all_participants(
    deps: SagaDeps, manifest: Manifest, *, verify_prefix: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run `subject.verify` across every manifest participant.

    Returns ``(receipts, unexpected)``. A participant registered as
    ``expects_residual`` may legitimately report artifacts remaining (the SES
    suppression entry, Iceberg rows inside the snapshot window) — anything else that
    remains is *unexpected* and the caller escalates it. The distinction comes from
    the registry, the same source conformance grades against, so the saga and the
    test suite cannot disagree about which residuals are honest.
    """
    receipts: dict[str, Any] = {}
    unexpected: list[dict[str, Any]] = []
    for participant in manifest.participants:
        request = VerifyRequest(subject_ref=manifest.subject_ref, saga_id=manifest.saga_id)
        body = deps.participants.call(participant.system_id, "verify", request.digested_body())
        response = VerifyResponse.model_validate(body)
        receipts[receipt_key(verify_prefix, participant.system_id)] = body
        if response.clean:
            continue
        if registry_get(participant.system_id).expects_residual:
            continue
        unexpected.append(
            {
                "systemId": participant.system_id,
                "remainingCount": len(response.remaining),
            }
        )
    return receipts, unexpected
