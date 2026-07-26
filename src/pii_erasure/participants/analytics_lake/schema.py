"""The analytics lake's table definition, beside the SQL that depends on it.

Same gap as V8-9, one participant along: `handler.py` queries a Glue table that nothing in
this repo created. It had not failed yet only because `make seed` stopped at Aurora first.

**The table must be Iceberg**, and not only because the archetype is about Iceberg
snapshots. `UPDATE` and `DELETE` — which `soft_delete`, `restore` and `hard_delete` all
use — are simply not available on a plain external Glue table; Athena rejects them. A
non-Iceberg table would make three of the five verbs impossible while `discover` and
`verify` carried on working, so the participant would look half-alive rather than broken.

**`vacuum_max_snapshot_age_seconds` is the residual the participant discloses.** The
`PARTIAL` outcome names a window in days; this property is what makes that window real
rather than a claim. They are derived from one constant, and a unit test asserts they
still agree — a disclosed retention period the table does not honour would be a fabricated
reassurance handed to an approver, which is worse than disclosing nothing.
"""

from __future__ import annotations

from typing import Any

from pii_erasure.participants.analytics_lake.handler import SNAPSHOT_RETENTION_DAYS

SNAPSHOT_RETENTION_SECONDS = SNAPSHOT_RETENTION_DAYS * 24 * 60 * 60


#: Athena reports an existing table this way. Matched on the message because a DDL failure
#: arrives as a query in state FAILED with a reason string, not as a typed exception.
_ALREADY_EXISTS = "already exists"


def create_table_sql(*, database: str, table: str, location: str) -> str:
    """DDL for the events table, in Athena's **Iceberg** `CREATE TABLE` grammar.

    Two things about this statement are not free choices, and getting either wrong
    produces a parse error rather than a helpful message (V8-10):

    * **No `IF NOT EXISTS`.** It is absent from the documented Iceberg grammar. Including
      it routes the statement into Athena's generic `CREATE TABLE` parser, which expects
      properties in a `WITH (...)` clause and rejects `LOCATION` outright —
      ``mismatched input 'LOCATION'. Expecting: 'COMMENT', 'WITH'``. Idempotency is
      therefore handled by `ensure_table` catching "already exists", the same shape used
      for Cognito and SES in the generator.
    * **No `EXTERNAL`.** ``CREATE EXTERNAL TABLE`` fails with *External keyword not
      supported for table type ICEBERG*. Athena creates Iceberg tables directly.

    Verified against the Athena user guide rather than recalled, after the recalled form
    turned out to be the Hive one.
    """
    return (
        f"CREATE TABLE {database}.{table} ("
        " subject_ref string,"
        " event string,"
        " asdp_state string"
        ") "
        f"LOCATION '{location}' "
        "TBLPROPERTIES ("
        " 'table_type' = 'ICEBERG',"
        " 'format' = 'parquet',"
        f" 'vacuum_max_snapshot_age_seconds' = '{SNAPSHOT_RETENTION_SECONDS}'"
        ")"
    )


def ensure_table(runner: Any, *, database: str, table: str, location: str) -> str:
    """Create the table if it is not there. Returns the statement attempted.

    Converges rather than erroring, because `make seed` is re-run constantly (V8-8) — but
    via a caught error rather than `IF NOT EXISTS`, which the Iceberg grammar does not
    accept. An existing table is the declared state reached, not a failure.
    """
    statement = create_table_sql(database=database, table=table, location=location)
    try:
        runner(statement)
    except Exception as error:
        if _ALREADY_EXISTS not in str(error).lower():
            raise
    return statement
