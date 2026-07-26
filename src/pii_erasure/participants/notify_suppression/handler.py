"""`notify-suppression` — Amazon SES. **Some residual is legally required.**

This is the worked example for invariant 7, and the reason the contract has a `residual`
field at all. Nneka Lindqvist unsubscribed before she asked to be erased. Her address sits
on the SES account-level suppression list, and that entry is what stops her being emailed
again. Delete it as part of "erasing her" and the next campaign mails her — erasure that
causes the harm it was requested to prevent.

So `hard_delete` removes the contact and **deliberately leaves the suppression entry**,
returning `PARTIAL` with a populated `residual`. Not `APPLIED`. The contract makes the
honest answer the only constructible one: `MutationResponse` refuses `APPLIED` alongside a
residual, and refuses `PARTIAL` without one.

**What actually remains is the plaintext address, not a hash** (V8-1). SES v2 has no hash
in its API: `PutSuppressedDestination` requires an `EmailAddress` and
`GetSuppressedDestination` returns one. The repo said "suppression hash" in ten places for
months — a comfortable detail nobody had checked, in the file whose whole point is
disclosing residuals honestly.

Two distinct things follow, and conflating them is what caused the error:

* **What SES keeps**: the address, in plaintext, at account level.
* **How we are allowed to refer to it**: by digest. Invariant 5 forbids an address
  reaching a locator, a ledger entry, a log line or an Observability span, and the
  residual travels into all four. So the residual's locator is
  ``ses://suppression/sha256:…`` — the disclosure is complete, and the disclosure
  mechanism does not itself leak.

The address is **derived** from the pseudonymous handle rather than stored beside it, for
the same reason `vector-index` derives its keys: a side mapping table is a second source of
truth that can be lost independently of what it addresses. Seeded realistic PII lives in
`profile-store`, where a profile store's PII belongs.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pii_erasure.contract import (
    Archetype,
    Artifact,
    DiscoverRequest,
    DiscoverResponse,
    HardDeleteRequest,
    MutationResponse,
    Outcome,
    Residual,
    RestoreRequest,
    SoftDeleteRequest,
    VerifyRequest,
    VerifyResponse,
)
from pii_erasure.participants._base import (
    IdempotencyLog,
    Participant,
    deletability,
    discovery_evidence,
    dispatch,
    receipt_evidence,
)

SYSTEM_ID = "notify-suppression"

#: Reserved by RFC 2606 — it can never resolve, so a seeding accident cannot email a real
#: person. The local part is the pseudonymous handle, so the address carries no real PII.
SUPPRESSION_DOMAIN = "meridian.invalid"

CONTACT_KIND = "contact"
SUPPRESSION_KIND = "suppression-entry"

_NOT_FOUND = ("NotFoundException", "BadRequestException")


def subject_address(subject_ref: str) -> str:
    """The only mapping from subject to address. Deterministic, stored nowhere."""
    return f"{subject_ref}@{SUPPRESSION_DOMAIN}"


def _address_digest(address: str) -> str:
    return f"sha256:{hashlib.sha256(address.encode('utf-8')).hexdigest()}"


class NotifySuppression(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.RESIDUAL_BY_DESIGN

    #: Declared at plan time, so the approver sees the residual in the manifest rather
    #: than discovering it in a phase-3 receipt.
    undeletable_kinds = frozenset({SUPPRESSION_KIND})

    def __init__(self, contact_list: str, *, client: Any | None = None) -> None:
        self._contact_list = contact_list
        self._ses = client or boto3.client("sesv2")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        artifacts = self._artifacts(request.subject_ref)
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, (), undeletable_kinds=self.undeletable_kinds),
            artifacts=artifacts,
            evidence=discovery_evidence(
                {
                    "contactList": self._contact_list,
                    "addressDigest": _address_digest(subject_address(request.subject_ref)),
                }
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        """Never clean once suppression exists — and that is the correct answer (V8-3).

        Reporting `clean=True` here would claim an erasure this participant did not
        perform. The saga treats a disclosed, approved residual as an acceptable end
        state; it does not require every participant to reach zero.
        """
        remaining = self._artifacts(request.subject_ref)
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=remaining,
            evidence=discovery_evidence(
                {"contactList": self._contact_list, "verify": True},
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        """Unsubscribe from every topic. Reversible, and touches no suppression state."""
        contact = self._get_contact(request.subject_ref)
        if contact is None:
            return self._noop("no contact for this subject")

        self._ses.update_contact(
            ContactListName=self._contact_list,
            EmailAddress=subject_address(request.subject_ref),
            UnsubscribeAll=True,
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"unsubscribedAll": True}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        contact = self._get_contact(request.subject_ref)
        if contact is None:
            return self._noop("no contact for this subject")

        self._ses.update_contact(
            ContactListName=self._contact_list,
            EmailAddress=subject_address(request.subject_ref),
            UnsubscribeAll=False,
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            evidence=receipt_evidence({"resubscribed": True}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        """Delete the contact; keep the suppression entry; say so."""
        address = subject_address(request.subject_ref)
        affected = 0
        if self._get_contact(request.subject_ref) is not None:
            self._ses.delete_contact(ContactListName=self._contact_list, EmailAddress=address)
            affected = 1

        if self._get_suppression(request.subject_ref) is None:
            # Nothing legally retained for this subject, so nothing to disclose. APPLIED
            # is the honest answer here, and hedging to PARTIAL with an empty residual is
            # forbidden by the contract anyway.
            return MutationResponse(
                system_id=self.system_id,
                outcome=Outcome.APPLIED,
                affected=affected,
                evidence=receipt_evidence({"deletedContacts": affected, "suppressed": False}),
            )

        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.PARTIAL,
            affected=affected,
            residual=(
                Residual(
                    kind=SUPPRESSION_KIND,
                    locator=f"ses://suppression/{_address_digest(address)}",
                    count=1,
                    classification=("PII", "CONTACT"),
                    reason=(
                        "The SES account-level suppression list retains this address in "
                        "plaintext. It is what prevents the subject being emailed again; "
                        "deleting it would re-enable the contact they opted out of. "
                        "Referred to here by digest because invariant 5 forbids the "
                        "address itself entering a locator, ledger entry or log."
                    ),
                ),
            ),
            evidence=receipt_evidence({"deletedContacts": affected, "suppressed": True}),
        )

    # ── SES detail ───────────────────────────────────────────────────────────────────

    def _artifacts(self, subject_ref: str) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        if self._get_contact(subject_ref) is not None:
            artifacts.append(
                Artifact(
                    kind=CONTACT_KIND,
                    locator=f"ses://{self._contact_list}/"
                    f"{_address_digest(subject_address(subject_ref))}",
                    count=1,
                    classification=("PII", "CONTACT"),
                )
            )
        if self._get_suppression(subject_ref) is not None:
            artifacts.append(
                Artifact(
                    kind=SUPPRESSION_KIND,
                    locator=f"ses://suppression/{_address_digest(subject_address(subject_ref))}",
                    count=1,
                    classification=("PII", "CONTACT"),
                )
            )
        return tuple(artifacts)

    def _noop(self, reason: str) -> MutationResponse:
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=0,
            evidence=receipt_evidence({"noop": reason}),
        )

    def _get_contact(self, subject_ref: str) -> dict[str, Any] | None:
        try:
            return dict(
                self._ses.get_contact(
                    ContactListName=self._contact_list,
                    EmailAddress=subject_address(subject_ref),
                )
            )
        except ClientError as error:
            if error.response["Error"]["Code"] in _NOT_FOUND:
                return None
            raise

    def _get_suppression(self, subject_ref: str) -> dict[str, Any] | None:
        try:
            return dict(
                self._ses.get_suppressed_destination(EmailAddress=subject_address(subject_ref))
            )
        except ClientError as error:
            if error.response["Error"]["Code"] in _NOT_FOUND:
                return None
            raise


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = NotifySuppression(os.environ["CONTACT_LIST_NAME"])
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
