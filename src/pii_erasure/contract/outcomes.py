"""What a participant reports back: the outcome of a mutation, and deletability.

`Outcome` is small on purpose. The interesting design choice is what it *lacks*: there is
no `SUCCESS_WITH_WARNINGS`, and no way to say "applied, mostly". A participant that could
not finish returns `PARTIAL` and populates `residual` — invariant 7. Silent partial success
is the failure mode that produces compliance incidents, so the contract makes it
unrepresentable rather than discouraged (enforced in `verbs.MutationResponse`).
"""

from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    """The result of `soft_delete`, `hard_delete` or `restore`."""

    #: Everything the request asked for was done. `residual` MUST be empty.
    APPLIED = "APPLIED"

    #: This exact `idempotencyKey` was already applied. Not an error: Lambda retries,
    #: at-least-once Scheduler delivery and checkpoint resume all replay calls (§4.3).
    ALREADY_APPLIED = "ALREADY_APPLIED"

    #: The participant declined — a missing or unrecognised `manifestDigest` or
    #: `approvalToken`, or a hold it will not cross. Nothing was changed.
    REFUSED = "REFUSED"

    #: Some of the work was done and some cannot be. `residual` MUST be populated and
    #: is disclosed to the approver and to the certificate.
    PARTIAL = "PARTIAL"


class Deletability(str, Enum):
    """Discovery's assessment of whether this system *can* honour the erasure."""

    #: Nothing found here for this subject. `verify` should agree.
    NOT_PRESENT = "NOT_PRESENT"

    #: Everything found can be deleted or shredded.
    DELETABLE = "DELETABLE"

    #: Some artifacts are deletable, some are not — the WORM and residual-by-design
    #: archetypes report this even on a healthy path.
    PARTIAL = "PARTIAL"

    #: A legal hold covers these artifacts. Erasure does not proceed here, and the
    #: hold is re-checked after the grace window rather than trusted from plan time.
    BLOCKED_BY_HOLD = "BLOCKED_BY_HOLD"
