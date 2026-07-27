"""Plan: obtain the manifest, sign it, validate it — the only node that may ever touch
the reasoning plane, and it does so by *receiving a manifest*, never by holding a model
client (invariant 2).

At M5 the manifest is provided in the start input — ADR-001's payoff: the saga replays
manifests, so it is fully testable before discovery exists. At M7 this node invokes the
AgentCore Runtime when no manifest is provided. Until then, an absent manifest fails
loudly; there is no stub planner and there must never be one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pii_erasure.manifest import Manifest, validate_manifest, with_digest
from pii_erasure.saga.deps import SagaDeps
from pii_erasure.saga.ordering import validate_order


class PlanSourceUnavailableError(RuntimeError):
    """No manifest was provided and no discovery Runtime exists yet (lands at M7)."""


class PlanMismatchError(ValueError):
    """The provided manifest is for a different saga or subject than this run."""


def make_plan(deps: SagaDeps) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def plan(state: dict[str, Any]) -> dict[str, Any]:
        provided = state.get("provided_manifest")
        if provided is None:
            raise PlanSourceUnavailableError(
                "no manifest provided — the discovery Runtime that synthesises one "
                "lands at M7; an M5 saga replays a hand-written manifest (ADR-001)"
            )
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
