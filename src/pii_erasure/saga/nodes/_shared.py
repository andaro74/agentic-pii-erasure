"""Helpers shared by the executor nodes. As deterministic as the nodes themselves."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pii_erasure.contract import VerifyRequest, VerifyResponse
from pii_erasure.contract.registry import get as registry_get
from pii_erasure.manifest import Manifest
from pii_erasure.observability.metrics import emit

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


def parse_iso(text: str) -> datetime:
    """The inverse of `iso`, on 3.10.

    `datetime.fromisoformat` only accepts a trailing ``Z`` from 3.11, and
    `requires-python` here is 3.10 — so the offset is normalised rather than assumed to
    parse. A value carrying no offset at all is read as UTC: this platform writes
    nothing else, and there is no second plausible reading of a saga timestamp.
    """
    normalised = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    moment = datetime.fromisoformat(normalised)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def elapsed_seconds(state: Mapping[str, Any], now: datetime) -> float | None:
    """Seconds since intake accepted this request, or `None` if that is unknown.

    `None` is returned for a checkpoint written before `started_at` existed. Clock skew
    between the intake invocation and this one is clamped to zero — sub-second skew
    genuinely is "instant", and a negative duration is not a number CloudWatch can hold.
    """
    raw = state.get("started_at")
    if not raw:
        return None
    return max(0.0, (now - parse_iso(str(raw))).total_seconds())


def emit_elapsed(deps: SagaDeps, state: Mapping[str, Any], metric: str) -> float | None:
    """Publish `metric` as seconds since intake — or publish **nothing** if unknown.

    The guard lives here rather than at each call site so there is exactly one place for
    it to be right, and no second duration metric can be added without it.

    Skipping the data point is the deliberate half, and it is not the same as emitting
    zero. A zero on a duration reads as *answered instantly*: it would pull a p99
    downwards, so a fleet of sagas that started before this field existed could hold
    `approval.time_to_decision` below its alarm threshold while the thing the alarm
    watches for was happening. Absent has to look absent.
    """
    seconds = elapsed_seconds(state, deps.now())
    if seconds is not None:
        emit(metric, seconds, deps.metric_dimensions("saga"))
    return seconds


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
