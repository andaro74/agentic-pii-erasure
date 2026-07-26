"""The manifest digest: what an approval actually binds to (ADR-006, invariant 3).

``digest = sha256(canonical(body))`` where *body* is the manifest's wire form minus
exactly three keys: ``provenance``, ``digest``, ``signature``. The exclusion is
structural — the keys are popped before canonicalisation — and backstopped: all three are
in `canonical.VOLATILE_KEYS`, so if a future refactor leaks one into the body, the
canonicaliser raises instead of silently digesting a moving target.

Enumeration order is not plan identity. Discovery may return participants and holds in
any order (pagination, concurrency), and a digest that varied with it would churn
approvals for semantically identical plans. So the body is **normalised before
canonicalisation**:

* ``participants`` sort by their `order` slot — ``(phase, rank, systemId)``. The slot is
  the *semantic* execution order and is itself digested, so reordering the actual plan
  still changes the digest (§8.3), while re-enumerating the same plan does not.
* ``legalHolds`` sort by ``holdId``; ``residualRisk`` by ``(kind, locator)``.
* Arrays inside each participant (``artifacts``, ``holds``, ``classification``) are
  already set-like in `canonical.py` and sort there. ``plannedOps`` deliberately is
  not — it is a sequence, and swapping soft/hard order is a different plan.

Normalisation lives here rather than in `canonical.py` because these are *manifest*
semantics; the canonicaliser stays frozen (invariant 4 — any change there is a
schemaVersion bump).
"""

from __future__ import annotations

import hashlib
from typing import Any

from pii_erasure.contract import canonical
from pii_erasure.manifest.models import Manifest

_EXCLUDED_KEYS = ("provenance", "digest", "signature")


class DigestMismatchError(ValueError):
    """The manifest's recorded digest does not match its body — it was edited after
    digesting, which is exactly what the digest exists to make detectable."""


def digested_body(manifest: Manifest) -> dict[str, Any]:
    """The normalised wire body the digest is computed over."""
    body: dict[str, Any] = manifest.model_dump(mode="json", by_alias=True)
    for key in _EXCLUDED_KEYS:
        body.pop(key, None)

    body["participants"] = sorted(
        body["participants"],
        key=lambda p: (p["order"]["phase"], p["order"]["rank"], p["systemId"]),
    )
    body["legalHolds"] = sorted(body["legalHolds"], key=lambda h: str(h["holdId"]))
    body["residualRisk"] = sorted(
        body["residualRisk"], key=lambda r: (str(r["kind"]), str(r["locator"]))
    )
    return body


def compute_digest(manifest: Manifest) -> str:
    return f"sha256:{hashlib.sha256(canonical(digested_body(manifest))).hexdigest()}"


def with_digest(manifest: Manifest) -> Manifest:
    """Return the manifest with its digest attached (or refreshed, if unsigned).

    Attaching the digest cannot change the digest — it is excluded from the body — and
    there is a test proving that, because circularity here would be quiet and fatal.
    """
    return manifest.model_copy(update={"digest": compute_digest(manifest)})


def assert_digest(manifest: Manifest) -> str:
    """Verify the recorded digest against the body; return it. Raises loudly otherwise."""
    if manifest.digest is None:
        raise DigestMismatchError("manifest carries no digest — call with_digest() first")
    recomputed = compute_digest(manifest)
    if manifest.digest != recomputed:
        # Deliberately does not echo either digest value's provenance — just the fact.
        raise DigestMismatchError(
            "manifest digest does not match its body — the manifest was modified after "
            "digesting; re-plan instead of editing (invariant 3)"
        )
    return recomputed
