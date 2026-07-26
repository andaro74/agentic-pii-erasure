"""Idempotency keys (ARCHITECTURE §4.3).

    idempotencyKey = SHA256(sagaId ‖ systemId ‖ operation ‖ canonical(artifacts))

This carries more weight in a serverless design than it did in a server-based one.
Lambda retries, EventBridge Scheduler's at-least-once delivery, checkpoint resume after a
crash, and operator re-runs all produce duplicate participant calls — and phase 3 has no
compensation to fall back on (invariant 6). A participant that sees a key it has already
applied returns `ALREADY_APPLIED` and does not act twice.

The ``‖`` above is realised as NUL-separated concatenation, and every component is
checked for NUL. Plain concatenation would make ``("sag", "a")`` and ``("sa", "ga")``
collide into one key — two different sagas sharing an idempotency record, so the second
one's deletion silently returns `ALREADY_APPLIED` having deleted nothing. There is a test
for exactly that pair.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pii_erasure.contract.canonical import canonical
from pii_erasure.contract.verbs import Artifact, Verb

_SEPARATOR = b"\x00"


class IdempotencyKeyError(ValueError):
    """Raised when a key cannot be derived unambiguously."""


def idempotency_key(
    *,
    saga_id: str,
    system_id: str,
    operation: Verb,
    artifacts: Sequence[Artifact],
) -> str:
    """Derive the key for one participant call. Deterministic; safe to recompute.

    Returns ``sha256:<hex>``. Two calls that would do the same work to the same system in
    the same saga produce the same key — including when discovery returned the artifacts
    in a different order, because `canonical()` sorts artifact arrays.
    """
    components = {"sagaId": saga_id, "systemId": system_id}
    for name, value in components.items():
        if not value:
            raise IdempotencyKeyError(f"{name} must not be empty")
        if "\x00" in value:
            raise IdempotencyKeyError(f"{name} must not contain NUL")

    body = _SEPARATOR.join(
        [
            saga_id.encode("utf-8"),
            system_id.encode("utf-8"),
            operation.value.encode("utf-8"),
            # Wrapped in a one-key object rather than canonicalised as a bare list: the
            # set-like sort is keyed on the *field name*, so an unnamed array would keep
            # whatever order discovery happened to return and the key would depend on
            # DynamoDB pagination order. Wrapping is what makes the claim above true.
            canonical({"artifacts": [artifact.digested_body() for artifact in artifacts]}),
        ]
    )
    return f"sha256:{hashlib.sha256(body).hexdigest()}"
