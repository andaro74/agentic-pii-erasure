"""`cognito-identity` — Amazon Cognito. **Revoke first.**

The authoritative identity store is the one participant whose ordering is not a
preference. Deleting a user while their session is still valid leaves a window in which
the subject's own client keeps writing — new rows in `billing-ledger`, new objects in
`upload-bucket`, a new profile item — *after* discovery enumerated what existed. The saga
would then delete a set that was already stale and report success. That is not a race in
theory; it is the ordinary behaviour of a mobile app with a cached token.

So `soft_delete` does two things in a fixed order:

1. `AdminUserGlobalSignOut` — revokes every issued refresh token, so no new access token
   can be minted. This stops *new* writes.
2. `AdminDisableUser` — blocks re-authentication.

Sign-out first, because disabling a user does not invalidate tokens already issued: a
disabled user with a live access token can keep working until it expires. Doing it the
other way round looks equivalent and leaves exactly the window this participant exists to
close. `restore` reverses only step 2 — revoked tokens are gone for good, and pretending
otherwise would be a restore that claims more than it did.

`hard_delete` is `AdminDeleteUser`, which genuinely removes the user. This is the
archetype where deletion means what the word implies, and it is worth having exactly one
of those to compare the other seven against.

The username **is** the pseudonymous `subjectRef` (invariant 5), so no real identifier
reaches Cognito and no side mapping table is needed to find the user again.
"""

from __future__ import annotations

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

SYSTEM_ID = "cognito-identity"

_NOT_FOUND = "UserNotFoundException"


class CognitoIdentity(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.AUTHORITATIVE_IDENTITY

    def __init__(self, user_pool_id: str, *, client: Any | None = None) -> None:
        self._pool = user_pool_id
        self._idp = client or boto3.client("cognito-idp")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        user = self._get_user(request.subject_ref)
        artifacts: tuple[Artifact, ...] = ()
        if user is not None:
            artifacts = (
                Artifact(
                    kind="user",
                    locator=self._locator(request.subject_ref),
                    count=1,
                    classification=("PII", "IDENTIFIER"),
                ),
            )
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, ()),
            artifacts=artifacts,
            evidence=discovery_evidence(
                {"userPoolId": self._pool, "username": request.subject_ref}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        user = self._get_user(request.subject_ref)
        remaining = (
            ()
            if user is None
            else (Artifact(kind="user", locator=self._locator(request.subject_ref), count=1),)
        )
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=remaining,
            evidence=discovery_evidence(
                {"userPoolId": self._pool, "username": request.subject_ref, "verify": True}
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        if self._get_user(request.subject_ref) is None:
            return self._nothing_to_do("no such user")

        # Order is the lesson. Revoke, then disable.
        self._idp.admin_user_global_sign_out(UserPoolId=self._pool, Username=request.subject_ref)
        self._idp.admin_disable_user(UserPoolId=self._pool, Username=request.subject_ref)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"revoked": True, "disabled": True}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        if self._get_user(request.subject_ref) is None:
            return self._nothing_to_do("no such user")

        # Re-enables only. The global sign-out cannot be undone, and a restore that
        # claimed to have restored the sessions would be describing work it did not do.
        self._idp.admin_enable_user(UserPoolId=self._pool, Username=request.subject_ref)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            evidence=receipt_evidence({"enabled": True, "sessionsRestored": False}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        if self._get_user(request.subject_ref) is None:
            # Already gone is a successful end state, not an error: phase 3 retries, and
            # a retry that failed because the first attempt succeeded would stall the saga.
            return MutationResponse(
                system_id=self.system_id,
                outcome=Outcome.APPLIED,
                affected=0,
                evidence=receipt_evidence({"alreadyAbsent": True}),
            )

        self._idp.admin_delete_user(UserPoolId=self._pool, Username=request.subject_ref)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=1,
            evidence=receipt_evidence({"deletedUser": self._locator(request.subject_ref)}),
        )

    # ── Cognito detail ───────────────────────────────────────────────────────────────

    def _locator(self, subject_ref: str) -> str:
        return f"cognito://{self._pool}/{subject_ref}"

    def _nothing_to_do(self, reason: str) -> MutationResponse:
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=0,
            evidence=receipt_evidence({"noop": reason}),
        )

    def _get_user(self, subject_ref: str) -> dict[str, Any] | None:
        try:
            return dict(self._idp.admin_get_user(UserPoolId=self._pool, Username=subject_ref))
        except ClientError as error:
            if error.response["Error"]["Code"] == _NOT_FOUND:
                return None
            raise


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = CognitoIdentity(os.environ["USER_POOL_ID"])
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
