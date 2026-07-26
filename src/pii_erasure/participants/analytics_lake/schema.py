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


def create_table_sql(*, database: str, table: str, location: str) -> str:
    """DDL for the events table. Identifiers come from the stack, values from nowhere.

    `IF NOT EXISTS` because `make seed` is re-run constantly (V8-8) and applying the
    schema must converge like every other write.
    """
    return (
        f'CREATE TABLE IF NOT EXISTS "{database}"."{table}" ('
        "  subject_ref string,"
        "  event string,"
        "  asdp_state string"
        ") "
        f"LOCATION '{location}' "
        "TBLPROPERTIES ("
        "  'table_type' = 'ICEBERG',"
        "  'format' = 'parquet',"
        f"  'vacuum_max_snapshot_age_seconds' = '{SNAPSHOT_RETENTION_SECONDS}'"
        ")"
    )


def ensure_table(runner: Any, *, database: str, table: str, location: str) -> str:
    """Apply the DDL through a caller-supplied Athena runner. Returns the statement."""
    statement = create_table_sql(database=database, table=table, location=location)
    runner(statement)
    return statement
