"""Saga state schema and reducers — the correctness surface (invariant 10).

Reducers decide how concurrent node writes merge. Get one wrong — last-write-wins on a
collection, say — and two participants' results silently overwrite each other, which
surfaces as a **recall failure**, not a crash (ADR-008). So the rules here are strict:

* collections merge or append, never overwrite;
* a genuine conflict — two different values for the same key — **raises** rather than
  picking a winner, because either winner would be silently wrong;
* an *identical* duplicate is absorbed, because at-least-once delivery (Lambda retries,
  Scheduler wakes, checkpoint resume re-executing a node) legitimately replays writes.

Every reducer below has a concurrent-write unit test in
`tests/unit/test_saga_state.py`, exercised both directly and through a parallel
two-branch `StateGraph` superstep.

This module imports `langgraph` typing helpers only via `Annotated` metadata — the
reducers themselves are plain functions so they stay unit-testable without a graph.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


class ReducerConflictError(ValueError):
    """Two concurrent writes disagreed about the same fact.

    Never resolved by picking a winner: the saga's facts (a receipt, a digest, a hold)
    have exactly one true value, and a disagreement means a bug upstream. Failing loudly
    here is what keeps the failure from surfacing later as a wrong recall number or a
    wrong approval.
    """


def merge_unique(current: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    """Key-wise union. Identical re-writes are absorbed; conflicting ones raise.

    Used for receipts and verify results keyed by ``"<verb>:<systemId>"`` — the shape
    under which two participants' concurrent writes must never clobber each other.
    """
    current = current or {}
    incoming = incoming or {}
    merged = dict(current)
    for key, value in incoming.items():
        if key in merged and merged[key] != value:
            raise ReducerConflictError(
                f"conflicting values for {key!r} — concurrent writes disagree, and "
                "picking either one silently would be the invariant-10 failure mode"
            )
        merged[key] = value
    return merged


def append_unique(current: list[Any] | None, incoming: list[Any] | None) -> list[Any]:
    """Append preserving order, absorbing exact duplicates.

    Duplicates are legitimate — a re-executed node re-emits its writes — so equality
    (not identity) dedupes. Order of first appearance is preserved so ledger-facing
    lists stay deterministic.
    """
    current = current or []
    incoming = incoming or []
    merged = list(current)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


def set_once(current: Any | None, incoming: Any | None) -> Any:
    """Write-once. A second write must agree exactly, or it raises.

    Used for the manifest and its digest: invariant 3 binds approval to the digest, so
    a mid-saga change of either is never a merge — it is an attack or a bug, and both
    deserve an exception, not a quiet overwrite.

    "Unset" is falsy, not just None: langgraph's binary-operator channels initialise
    with the annotated type's default (``str()`` is ``""``, ``dict()`` is ``{}``) —
    verified against the installed 1.2.9, and the reducer tests pin it.
    """
    if incoming is None:
        return current
    if current and current != incoming:
        raise ReducerConflictError(
            "attempted to overwrite a write-once value — a manifest (or its digest) "
            "never changes mid-saga; re-planning creates a new manifest (invariant 3)"
        )
    return incoming


def last_value(current: Any | None, incoming: Any | None) -> Any:
    """Explicit last-write for *scalars that legitimately progress* (status, phase).

    Deliberately named and reduced rather than left to the framework default, so the
    choice is visible and tested: this is only ever attached to single-writer scalars,
    never to a collection.
    """
    return current if incoming is None else incoming


class SagaState(TypedDict, total=False):
    """The saga's complete state. Lives in the checkpointer and nowhere else."""

    # ── identity (write-once) ────────────────────────────────────────────────
    saga_id: Annotated[str, set_once]
    subject_ref: Annotated[str, set_once]  # pseudonymous handle — never raw PII
    request_id: Annotated[str, set_once]
    tenant_id: Annotated[str, set_once]

    # ── the plan (write-once; invariant 3) ───────────────────────────────────
    #: The manifest handed in at start — M5's hand-written fixture (ADR-001: the saga
    #: replays manifests, so it is fully testable before discovery exists). At M7 the
    #: plan node invokes the Runtime instead when this is absent.
    provided_manifest: Annotated[dict[str, Any] | None, set_once]
    #: The signed manifest as its camelCase JSON dump — a dict, not a Manifest, so the
    #: checkpoint serialization never depends on pydantic internals across versions.
    manifest: Annotated[dict[str, Any] | None, set_once]
    manifest_digest: Annotated[str | None, set_once]

    # ── progress (single-writer scalars) ─────────────────────────────────────
    phase: Annotated[int, last_value]
    status: Annotated[str, last_value]
    #: Set by hard_delete's final pass, after every receipt is in. The router loops
    #: the phase-3 node until this is true, so the tombstone write is its own
    #: checkpointed superstep rather than a tail rider on the last deletion.
    tombstoned: Annotated[bool, last_value]

    # ── collections (merge/append; invariant 10) ─────────────────────────────
    #: Mutation + verify responses keyed "<verb>:<systemId>" — e.g.
    #: "soft_delete:profile-store". The merge raises on conflict.
    receipts: Annotated[dict[str, Any], merge_unique]
    #: Restore tokens from soft_delete, keyed by systemId. Consumed by compensate.
    restore_tokens: Annotated[dict[str, Any], merge_unique]
    #: Holds observed at plan time or re-check, deduplicated by value.
    holds: Annotated[list[dict[str, Any]], append_unique]
    #: Residuals disclosed by participants (invariant 7), carried to the certificate.
    residuals: Annotated[list[dict[str, Any]], append_unique]
    #: Structured error records: {"node", "systemId", "error"}.
    errors: Annotated[list[dict[str, Any]], append_unique]
    #: Wake reasons already scheduled, so re-executed nodes do not double-schedule.
    schedules: Annotated[list[str], append_unique]
    #: Sweep wake reasons already completed (sweep_t7, sweep_t30).
    sweeps_done: Annotated[list[str], append_unique]
    #: Systems whose soft_delete was compensated (restored). Phase 2 only.
    compensated: Annotated[list[str], append_unique]

    # ── approval (write-once once granted) ───────────────────────────────────
    #: {"decision", "approver", "digest", "token"} — token is digest-bound (ADR-006).
    approval: Annotated[dict[str, Any] | None, set_once]


#: Terminal statuses. One place, so edges and tests agree on the vocabulary.
STATUS_COMPLETED = "completed"
STATUS_COMPENSATED = "compensated"
STATUS_BLOCKED = "blocked"
STATUS_STUCK = "stuck"  # phase 3 halt: SQS DLQ raised, forward recovery only
STATUS_ABORTED = "aborted"  # post-approval digest mismatch — nothing irreversible ran
STATUS_ALREADY_TOMBSTONED = "already_tombstoned"
STATUS_RUNNING = "running"
