"""Plan: obtain the manifest, sign it, validate it — the only node that may ever touch
the reasoning plane, and it does so by *receiving a manifest*, never by holding a model
client (invariant 2).

Two sources, one shape. A manifest **provided in the start input** is replayed (ADR-001's
payoff: the execution plane was fully testable before discovery existed). Otherwise the
node **invokes the discovery Runtime and receives a manifest body** (M7). Neither path
puts a model client in this process, and there is no third path — an absent manifest with
no planner configured fails loudly, because a stub planner here would be a plan nobody
made, signed with the real CMK.

The asymmetry worth noticing: discovery *proposes*, the saga *signs*. The Runtime returns
participants and holds; this node adds the digest, the KMS signature, and the ordering
validation. So a compromised or confused reasoning plane can propose a bad plan, and it
still has to survive `validate_manifest`, `validate_order`, and a human approver bound to
the digest (ADR-006) before anything irreversible happens.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pii_erasure.manifest import Manifest, validate_manifest, with_digest
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.ordering import validate_order


class PlanSourceUnavailableError(RuntimeError):
    """Neither a provided manifest nor a configured discovery Runtime.

    Fails loudly rather than degrading. A stub planner here would produce a plan
    nobody made and hand it the real CMK — the exact "no stub that pretends success"
    rule this repo builds under.
    """


class PlanMismatchError(ValueError):
    """The provided manifest is for a different saga or subject than this run."""


class DiscoveryIncompleteError(RuntimeError):
    """Discovery could not answer for every system it probed.

    Fail closed. A failed probe is not an empty one, and a manifest built while one is
    outstanding certifies erasure for a system nobody successfully looked at — a
    false negative, which is the class caught by nobody (ADR-008).
    """


def build_manifest_body(
    discovery: dict[str, Any], *, request_id: str, grace_window_days: int = 30
) -> dict[str, Any]:
    """Turn a Runtime discovery response into an unsigned manifest body.

    Deliberately mechanical: field mapping and nothing else. Every judgement the
    manifest encodes — what was found, what is held, what order to run in — was made
    in the reasoning plane and is *validated* downstream by `validate_manifest` and
    `validate_order`. If this function ever needs a decision, that decision belongs on
    one side of the boundary or the other, not in the translation between them.
    """
    if discovery.get("incomplete"):
        raise DiscoveryIncompleteError(
            f"discovery did not complete for {sorted(discovery['incomplete'])} — "
            "a failed probe is not an empty one"
        )
    saga_id = discovery["sagaId"]
    return {
        "manifestId": f"man_{saga_id}",
        "sagaId": saga_id,
        "subjectRef": discovery["subjectRef"],
        "requestId": request_id,
        "provenance": {
            "discoveredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "agentVersion": discovery["provenance"]["agentVersion"],
            "modelId": discovery["provenance"].get("modelId"),
            # Volatile, and excluded from the digested body by canonicalisation
            # (invariant 4) — carried for correlation, never for identity.
            "runtimeSessionId": discovery["provenance"].get("runtimeSessionId"),
        },
        "participants": discovery["participants"],
        "legalHolds": discovery.get("legalHolds", []),
        "graceWindowDays": grace_window_days,
    }


def make_plan(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def plan(state: dict[str, Any]) -> dict[str, Any]:
        provided = state.get("provided_manifest")
        if provided is None:
            if deps.planner is None:
                raise PlanSourceUnavailableError(
                    "no manifest provided and no discovery Runtime configured — set "
                    "DISCOVERY_RUNTIME_ARN, or supply a manifest in the start input. "
                    "There is no stub planner and there must never be one."
                )
            body = deps.planner.plan(
                subject_ref=state["subject_ref"],
                saga_id=state["saga_id"],
                tenant=state.get("tenant", "default"),
            )
            provided = build_manifest_body(body, request_id=state["request_id"])
        manifest = Manifest.model_validate(provided)

        if manifest.saga_id != state["saga_id"] or manifest.subject_ref != state["subject_ref"]:
            raise PlanMismatchError(
                "manifest names a different saga or subject than the start input — "
                "executing someone else's plan is the one mistake this check exists for"
            )

        if manifest.digest is None:
            manifest = with_digest(manifest)
        if manifest.signature is None:
            manifest = deps.signer.sign(manifest)

        validate_manifest(manifest, signer=deps.signer, trusted_key_arns=deps.trusted_key_arns)
        validate_order(manifest)

        digest = manifest.digest
        assert digest is not None  # validate_manifest guarantees it; mypy cannot see that
        deps.ledger.append(
            saga_id=manifest.saga_id,
            event_type="MANIFEST_SIGNED",
            body={
                "manifestId": manifest.manifest_id,
                "manifestDigest": digest,
                "participantCount": len(manifest.participants),
            },
        )
        return {
            "manifest": manifest.model_dump(mode="json", by_alias=True),
            "manifest_digest": digest,
        }

    return plan
