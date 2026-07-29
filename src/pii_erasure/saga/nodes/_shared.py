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


def disclosed_residuals(receipts: Any, system_id: str) -> dict[str, int]:
    """``{locator: count}`` this participant *itself* disclosed when it hard-deleted.

    Read from its own phase-3 receipt, so the yardstick is the participant's own
    statement rather than a second opinion the platform keeps about it.
    """
    receipt = (receipts or {}).get(receipt_key("hard_delete", system_id)) or {}
    disclosed: dict[str, int] = {}
    for residual in receipt.get("residual") or ():
        locator = str(residual.get("locator", ""))
        disclosed[locator] = max(disclosed.get(locator, 0), int(residual.get("count", 1)))
    return disclosed


def verify_all_participants(
    deps: SagaDeps,
    manifest: Manifest,
    *,
    verify_prefix: str,
    receipts_so_far: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run `subject.verify` across every manifest participant.

    Returns ``(receipts, unexpected)``. Remaining artifacts are expected — not a failed
    erasure — in two cases:

    1. The participant is registered as ``expects_residual``: its residue is structural
       (the SES suppression entry, Iceberg rows inside the snapshot window). The registry
       is the same source conformance grades against, so the saga and the test suite
       cannot disagree about which residuals are honest.
    2. **The participant disclosed exactly this residue when it hard-deleted.** A scoped
       legal hold makes any participant a residual-bearing one for one saga (ADR-027):
       `billing-ledger` retains `public.invoices` under Art. 17(3)(e), returns PARTIAL
       naming it, and then reports it — correctly — as remaining. Grading that against
       the registry flag alone declared a *legally required* retention a failed erasure
       and halted the saga at the T+0 verify, which is the path ADR-027 exists to allow
       (V12-2). Invariant 7 requires participants to disclose residue honestly; the
       platform's half of that bargain is to treat what was disclosed as disclosed.

    Anything else remaining is escalated by the caller. The count is compared too: more
    rows under a held locator than the hold retained is a reappearance, not a retention.
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
        disclosed = disclosed_residuals(receipts_so_far, participant.system_id)
        surplus = [
            artifact
            for artifact in response.remaining
            if artifact.count > disclosed.get(artifact.locator, 0)
        ]
        if not surplus:
            continue
        unexpected.append(
            {
                "systemId": participant.system_id,
                "remainingCount": len(surplus),
            }
        )
    return receipts, unexpected
