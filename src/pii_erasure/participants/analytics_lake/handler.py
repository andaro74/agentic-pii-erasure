"""`analytics-lake` — S3 + Glue + Athena over Apache Iceberg.

**You cannot delete a row from a Parquet file.** Parquet objects are immutable. Iceberg's
`DELETE FROM` does not reach into them — it writes a *new* snapshot in which the row is
absent, and the old snapshot, still pointing at the original data files, remains valid and
queryable by timestamp or snapshot id. Time travel is the feature; it is also the reason
this participant cannot honestly report `APPLIED`.

The rows are genuinely gone from the current table state, so a query returns nothing and a
dashboard looks correct. They are equally genuinely still on disk until snapshot expiry
runs and the orphan files are cleaned up. `hard_delete` therefore returns `PARTIAL` with a
residual naming the snapshot window — the second worked example of invariant 7, and the
one where the residual is *temporary* rather than permanent. `notify-suppression` keeps its
entry forever; here the residual has an expiry date, which the approver should see.

Expiring snapshots on the spot is deliberately **not** done. `expire_snapshots` rewrites
table metadata for every subject at once, is expensive, and would make one subject's
erasure a global maintenance operation with its own failure modes. Disclosing a bounded
window is more honest than hiding an unbounded operation inside a per-subject verb.

**Athena is asynchronous**, so every statement here is start → poll → read, with a bounded
wait. A timeout raises rather than returning a hopeful `APPLIED`: an erasure whose result
is unknown must not be recorded as an erasure that happened.

**No SQL is built from a subject reference.** Athena's `ExecutionParameters` binds `?`
placeholders, so the statements are fixed strings.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import Any

import boto3

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

SYSTEM_ID = "analytics-lake"

SNAPSHOT_KIND = "iceberg-snapshot"
ROW_KIND = "row"

#: Iceberg's snapshot retention. Must match the table property set in the stack —
#: disclosing a window the table does not honour would be a fabricated reassurance.
SNAPSHOT_RETENTION_DAYS = 7

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}

#: Bounded, because the Lambda is bounded. Athena statements here touch one partition and
#: complete in seconds; a minute means something is wrong and the caller must hear about it.
_MAX_WAIT_SECONDS = 45.0
_POLL_SECONDS = 0.5


#: Catalog identifiers cannot be bound as parameters — only *values* can — so the database
#: and table names are interpolated into the SQL. They come from the stack rather than from
#: a caller, but "it is trusted" is an assertion, not a mechanism. This is the mechanism:
#: anything that is not a plain identifier fails at construction, before a statement is
#: ever built. That is what makes the `S608` suppressions below honest rather than a
#: silenced warning.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class AthenaTimeoutError(RuntimeError):
    """A statement did not finish inside the budget. Never reported as a successful erasure."""


def _identifier(value: str, *, field: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(
            f"{field}={value!r} is not a plain SQL identifier — refusing to build a "
            "statement around it"
        )
    return value


class AnalyticsLake(Participant):
    system_id = SYSTEM_ID
    archetype = Archetype.COLUMNAR_ANALYTICS

    undeletable_kinds = frozenset({SNAPSHOT_KIND})

    def __init__(
        self,
        database: str,
        table: str,
        workgroup: str,
        output_location: str,
        *,
        client: Any | None = None,
        clock: Any = None,
    ) -> None:
        self._database = _identifier(database, field="ATHENA_DATABASE")
        self._table = _identifier(table, field="ATHENA_TABLE")
        self._workgroup = workgroup
        self._output = output_location
        self._athena = client or boto3.client("athena")
        self._today = clock or date.today

    # ── reads ────────────────────────────────────────────────────────────────────────

    def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        rows = self._row_count(request.subject_ref)
        artifacts: tuple[Artifact, ...] = ()
        if rows:
            artifacts = (
                Artifact(
                    kind=ROW_KIND,
                    locator=self._locator(),
                    count=rows,
                    classification=("PII", "BEHAVIOURAL"),
                ),
            )
        return DiscoverResponse(
            system_id=self.system_id,
            archetype=self.archetype,
            found=bool(artifacts),
            deletability=deletability(artifacts, (), undeletable_kinds=self.undeletable_kinds),
            artifacts=artifacts,
            evidence=discovery_evidence(
                {"database": self._database, "table": self._table, "rows": rows}
            ),
        )

    def verify(self, request: VerifyRequest) -> VerifyResponse:
        """Never `clean`, and the reason is worth stating precisely.

        This participant cannot distinguish "the subject was never here" from "the subject
        was deleted and earlier snapshots still hold the rows" without walking the table's
        snapshot history by time travel — a full scan per snapshot, per subject. So it
        reports the conservative answer: not clean, with the window disclosed.

        That over-discloses for a subject who was never present. Erring toward *claiming a
        residual that is not there* rather than *missing one that is* is the correct
        direction for a privacy control, and it keeps `verify` and `hard_delete` telling
        the same story (V8-3) instead of one contradicting the other.

        `retentionUntil` is an **upper bound**: verify does not know when the delete
        happened, so it dates the window from now. A window that reads longer than it is
        cannot cause an early "all clear".
        """
        rows = self._row_count(request.subject_ref)
        remaining: list[Artifact] = []
        if rows:
            remaining.append(Artifact(kind=ROW_KIND, locator=self._locator(), count=rows))
        else:
            remaining.append(
                Artifact(
                    kind=SNAPSHOT_KIND,
                    locator=self._snapshot_locator(),
                    count=1,
                    retention_until=self._window_expiry(),
                )
            )
        return VerifyResponse(
            system_id=self.system_id,
            clean=False,
            remaining=tuple(remaining),
            evidence=discovery_evidence(
                {"database": self._database, "table": self._table, "verify": True}
            ),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────

    def soft_delete(self, request: SoftDeleteRequest) -> MutationResponse:
        affected = self._row_count(request.subject_ref)
        self._run(
            f"UPDATE {self._qualified()} SET asdp_state = ? WHERE subject_ref = ?",  # noqa: S608
            ["pending-delete", request.subject_ref],
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=affected,
            restore_token=f"{self.system_id}:{request.saga_id}",
            evidence=receipt_evidence({"marked": affected}),
        )

    def restore(self, request: RestoreRequest) -> MutationResponse:
        affected = self._row_count(request.subject_ref)
        self._run(
            f"UPDATE {self._qualified()} SET asdp_state = NULL WHERE subject_ref = ?",  # noqa: S608
            [request.subject_ref],
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.APPLIED,
            affected=affected,
            evidence=receipt_evidence({"unmarked": affected}),
        )

    def hard_delete(self, request: HardDeleteRequest) -> MutationResponse:
        affected = self._row_count(request.subject_ref)
        self._run(
            f"DELETE FROM {self._qualified()} WHERE subject_ref = ?",  # noqa: S608
            [request.subject_ref],
        )
        return MutationResponse(
            system_id=self.system_id,
            outcome=Outcome.PARTIAL,
            affected=affected,
            residual=(
                Residual(
                    kind=SNAPSHOT_KIND,
                    locator=self._snapshot_locator(),
                    count=1,
                    classification=("PII", "BEHAVIOURAL"),
                    retention_until=self._window_expiry(),
                    reason=(
                        "The rows are absent from the current Iceberg snapshot, but "
                        "earlier snapshots still reference the Parquet data files that "
                        "contain them, and remain queryable by time travel until "
                        f"snapshot expiry ({SNAPSHOT_RETENTION_DAYS} days). Expiring "
                        "snapshots is a whole-table maintenance operation and is not run "
                        "per subject."
                    ),
                ),
            ),
            evidence=receipt_evidence({"deletedRows": affected, "snapshotResidual": True}),
        )

    # ── Athena detail ────────────────────────────────────────────────────────────────

    def _qualified(self) -> str:
        return f'"{self._database}"."{self._table}"'

    def _locator(self) -> str:
        return f"awsdatacatalog://{self._database}/{self._table}"

    def _snapshot_locator(self) -> str:
        return f"{self._locator()}#snapshots"

    def _window_expiry(self) -> str:
        return (self._today() + timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()

    def _row_count(self, subject_ref: str) -> int:
        rows = self._run(
            f"SELECT count(*) FROM {self._qualified()} WHERE subject_ref = ?",  # noqa: S608
            [subject_ref],
        )
        # Row 0 is the header when metadata is included; the count is the first data cell.
        if len(rows) < 2:
            return 0
        cell = rows[1]["Data"][0]
        return int(cell.get("VarCharValue") or 0)

    def _run(self, sql: str, parameters: list[str]) -> list[dict[str, Any]]:
        started = self._athena.start_query_execution(
            QueryString=sql,
            ExecutionParameters=parameters,
            QueryExecutionContext={"Database": self._database},
            WorkGroup=self._workgroup,
            ResultConfiguration={"OutputLocation": self._output},
        )
        execution_id = started["QueryExecutionId"]

        deadline = time.monotonic() + _MAX_WAIT_SECONDS
        while True:
            status = self._athena.get_query_execution(QueryExecutionId=execution_id)[
                "QueryExecution"
            ]["Status"]
            state = status["State"]
            if state in _TERMINAL:
                break
            if time.monotonic() > deadline:
                raise AthenaTimeoutError(
                    f"{self.system_id}: statement {execution_id} still {state} after "
                    f"{_MAX_WAIT_SECONDS}s — refusing to report an outcome it does not know"
                )
            time.sleep(_POLL_SECONDS)

        if state != "SUCCEEDED":
            raise RuntimeError(
                f"{self.system_id}: statement {execution_id} ended {state}: "
                f"{status.get('StateChangeReason', 'no reason given')}"
            )

        results = self._athena.get_query_results(QueryExecutionId=execution_id)
        rows: list[dict[str, Any]] = results["ResultSet"]["Rows"]
        return rows


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    participant = AnalyticsLake(
        os.environ["ATHENA_DATABASE"],
        os.environ["ATHENA_TABLE"],
        os.environ["ATHENA_WORKGROUP"],
        os.environ["ATHENA_OUTPUT_LOCATION"],
    )
    log = IdempotencyLog(os.environ["IDEMPOTENCY_TABLE"])
    return dispatch(participant, event, context, idempotency=log)
