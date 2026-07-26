"""`billing-ledger` — Aurora PostgreSQL Serverless v2 via the RDS Data API.

**Referential integrity dictates ordering, and statutory retention beats erasure.**

Two things make the relational archetype different from every other participant here.

**Order is not a preference.** `invoice_lines` references `invoices` references
`customers`. Delete the customer first and the database refuses; delete in the right order
and it succeeds. A participant that got this wrong would fail loudly rather than silently,
which makes it the *easy* half — the ordering is encoded once, in `_DELETE_ORDER`, and
read top-down.

**A hold is an unconditional veto, and it is re-checked here.** Dmitri Vasquez-Lund has a
litigation hold scoped to `public.invoices`. GDPR Art. 17(3)(e) makes retention lawful
where it is necessary for legal claims, so erasing under a live hold is not a compliance
win — it is spoliation. The hold is re-read at execution rather than trusted from the
manifest, because a hold can be filed *during* the grace window (§5.3), and the plan was
approved before it existed.

The hold's scope matters as much as its existence: it covers `public.invoices`, not the
subject. Rows outside that scope are still erased, and the response is `PARTIAL` naming
exactly what was retained. Treating a scoped hold as subject-wide would silently
under-delete — a recall failure wearing a compliance costume.

**No SQL is ever assembled from a subject reference.** Every statement is a fixed string
with named parameters bound through the Data API's typed `parameters`. This is also why
there is no generic "run a query" tool: Cedar cannot express a constraint over arbitrary
SQL, so a participant that accepted one would void the policy layer.

**No VPC.** The Data API is an HTTPS endpoint, which is the entire reason Aurora is
reachable from a Lambda that attaches to no network — asserted at synth time.
"""

from __future__ import annotations

import os
from typing import Any

import boto3

from pii_erasure.contract import (
    Archetype,
    Artifact,
    DiscoverRequest,
    DiscoverResponse,
    HardDeleteRequest,
    Hold,
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
    blocks,
    deletability,
    discovery_evidence,
    dispatch,
    receipt_evidence,
)

SYSTEM_ID = "billing-ledger"

#: Child before parent. The database enforces this; stating it once keeps every verb
#: consistent with the schema rather than with whoever wrote the verb.
_DELETE_ORDER: tuple[str, ...] = ("public.invoice_lines", "public.invoices", "public.customers")

#: Counted in discovery, in the same order, so `discover` and `hard_delete` cannot drift
#: apart about which tables constitute "the subject here".
_COUNT_SQL: dict[str, str] = {
    "public.customers": "SELECT count(*) FROM public.customers WHERE subject_ref = :subject_ref",
    "public.invoices": "SELECT count(*) FROM public.invoices WHERE subject_ref = :subject_ref",
    "public.invoice_lines": (
        "SELECT count(*) FROM public.invoice_lines l "
        "JOIN public.invoices i ON i.invoice_id = l.invoice_id "
        "WHERE i.subject_ref = :subject_ref"
    ),
}

_DELETE_SQL: dict[str, str] = {
    "public.customers": "DELETE FROM public.customers WHERE subject_ref = :subject_ref",
    "public.invoices": "DELETE FROM public.invoices WHERE subject_ref = :subject_ref",
    "public.invoice_lines": (
        "DELETE FROM public.invoice_lines l USING public.invoices i "
        "WHERE i.invoice_id = l.invoice_id AND i.subject_ref = :subject_ref"
    ),
}

_HOLDS_SQL = (
    "SELECT hold_id, authority, scope, basis, expires_at FROM public.legal_holds "
    "WHERE subject_ref = :subject_ref"
)

_MARK_SQL = "UPDATE public.invoices SET pending_delete = :pending WHERE subject_ref = :subject_ref"


class BillingLedger(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.RELATIONAL

    def __init__(
        self,
        cluster_arn: str,
        secret_arn: str,
        database: str,
        *,
        client: Any | None = None,
    ) -> None:
        self._cluster = cluster_arn
        self._secret = secret_arn
        self._database = database
        self._data = client or boto3.client("rds-data")

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        counts = self._counts(request.subject_ref)
        holds = self._holds(request.subject_ref)
        artifacts = tuple(
            Artifact(
                kind="row",
                locator=table,
                count=count,
                classification=("PII", "FINANCIAL"),
            )
            for table, count in counts.items()
            if count
        )
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, holds),
            artifacts=artifacts,
            holds=holds,
            evidence=discovery_evidence(
                {"database": self._database, "tables": sorted(_COUNT_SQL), "holds": len(holds)}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        counts = self._counts(request.subject_ref)
        remaining = tuple(
            Artifact(kind="row", locator=table, count=count)
            for table, count in counts.items()
            if count
        )
        return VerifyResponse(
            system_id=self.system_id,
            clean=not remaining,
            remaining=remaining,
            evidence=discovery_evidence(
                {"database": self._database, "verify": True},
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        result = self._execute(_MARK_SQL, subject_ref=request.subject_ref, pending=True)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=int(result.get("numberOfRecordsUpdated", 0)),
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"marked": True}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        result = self._execute(_MARK_SQL, subject_ref=request.subject_ref, pending=False)
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=int(result.get("numberOfRecordsUpdated", 0)),
            evidence=receipt_evidence({"unmarked": True}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        # Re-read holds. The manifest was approved before the grace window; this is the
        # first moment that reflects the window's end (§5.3).
        holds = self._holds(request.subject_ref)
        counts = self._counts(request.subject_ref)
        held = [table for table in _DELETE_ORDER if blocks(holds, table) and counts.get(table)]
        deletable = [
            table for table in _DELETE_ORDER if not blocks(holds, table) and counts.get(table)
        ]

        if held and not deletable:
            # An unconditional veto. Nothing was touched, and the response says so rather
            # than reporting a successful erasure of zero rows.
            return MutationResponse(
                system_id=self.system_id,
                outcome=Outcome.REFUSED,
                affected=0,
                evidence=receipt_evidence(
                    {"blockedByHold": sorted({hold.hold_id for hold in holds})}
                ),
            )

        affected = 0
        for table in deletable:  # child before parent
            result = self._execute(_DELETE_SQL[table], subject_ref=request.subject_ref)
            affected += int(result.get("numberOfRecordsUpdated", 0))

        if not held:
            return MutationResponse(
                system_id=self.system_id,
                outcome=Outcome.APPLIED,
                affected=affected,
                evidence=receipt_evidence({"deletedFrom": deletable}),
            )

        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.PARTIAL,
            affected=affected,
            residual=tuple(
                Residual(
                    kind="row",
                    locator=table,
                    count=counts[table],
                    classification=("PII", "FINANCIAL"),
                    reason=(
                        "Retained under a live legal hold "
                        f"({', '.join(sorted(h.hold_id for h in holds if blocks([h], table)))}). "
                        "GDPR Art. 17(3)(e) makes retention lawful where it is necessary "
                        "for the establishment or defence of legal claims."
                    ),
                )
                for table in held
            ),
            evidence=receipt_evidence({"deletedFrom": deletable, "retained": held}),
        )

    # ── Data API detail ──────────────────────────────────────────────────────────────

    def _execute(self, sql: str, **params: Any) -> dict[str, Any]:
        """One parameterised statement. Values are bound, never interpolated."""
        return dict(
            self._data.execute_statement(
                resourceArn=self._cluster,
                secretArn=self._secret,
                database=self._database,
                sql=sql,
                parameters=[
                    {"name": name, "value": _field(value)} for name, value in params.items()
                ],
                includeResultMetadata=True,
            )
        )

    def _counts(self, subject_ref: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table, sql in _COUNT_SQL.items():
            result = self._execute(sql, subject_ref=subject_ref)
            records = result.get("records") or [[{"longValue": 0}]]
            counts[table] = int(records[0][0].get("longValue", 0))
        return counts

    def _holds(self, subject_ref: str) -> tuple[Hold, ...]:
        result = self._execute(_HOLDS_SQL, subject_ref=subject_ref)
        holds: list[Hold] = []
        for record in result.get("records") or []:
            expires = record[4].get("stringValue") if not record[4].get("isNull") else None
            holds.append(
                Hold(
                    hold_id=str(record[0].get("stringValue", "")),
                    authority=str(record[1].get("stringValue", "")),
                    scope=str(record[2].get("stringValue", "")),
                    basis=str(record[3].get("stringValue", "")),
                    expires_at=expires,
                )
            )
        return tuple(holds)


def _field(value: Any) -> dict[str, Any]:
    """Map a Python value onto the Data API's typed `Field` union."""
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if value is None:
        return {"isNull": True}
    return {"stringValue": str(value)}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = BillingLedger(
        os.environ["DB_CLUSTER_ARN"],
        os.environ["DB_SECRET_ARN"],
        os.environ["DB_NAME"],
    )
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
