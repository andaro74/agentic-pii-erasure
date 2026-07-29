"""The billing ledger's SQL and its schema must describe the same database.

This exists because of V8-9: `handler.py` queried `public.customers` and nothing had ever
created it. The failure surfaced as `relation "public.customers" does not exist`, on a
deployed cluster, during `make seed` — the slowest place to learn that two files in the
same package disagree about what a table is called.

Nothing hermetic could catch it before, because the schema did not exist to compare
against. Now that it does, the comparison is cheap and the drift it prevents is the
ordinary kind: renaming a column in the DDL and missing one of the four fixed statements
that reference it.
"""

from __future__ import annotations

import re

import pytest

from pii_erasure.participants.billing_ledger import handler
from pii_erasure.participants.billing_ledger.schema import SCHEMA_STATEMENTS, ensure_schema

_DDL = "\n".join(SCHEMA_STATEMENTS).lower()

#: Every fixed statement the participant runs.
_PARTICIPANT_SQL = (
    tuple(handler._COUNT_SQL.values())
    + tuple(handler._DELETE_SQL.values())
    + (handler._HOLDS_SQL, handler._MARK_SQL)
)

#: Words that appear in the participant's SQL and are not column names.
_NOT_COLUMNS = frozenset(
    {
        "select",
        "count",
        "from",
        "where",
        "delete",
        "update",
        "set",
        "join",
        "on",
        "using",
        "and",
        "public",
        "customers",
        "invoices",
        "invoice_lines",
        "legal_holds",
        "l",
        "i",
    }
)


def _tables(sql: str) -> set[str]:
    return set(re.findall(r"public\.([a-z_]+)", sql.lower()))


def test_every_table_the_participant_queries_is_created() -> None:
    referenced: set[str] = set()
    for statement in _PARTICIPANT_SQL:
        referenced |= _tables(statement)
    assert referenced, "no tables extracted — the pattern is broken, not the schema"

    created = set(re.findall(r"create table if not exists public\.([a-z_]+)", _DDL))
    missing = sorted(referenced - created)
    assert not missing, (
        f"handler.py queries {missing} but the schema never creates them — `make seed` "
        f"would fail with 'relation does not exist' against a deployed cluster (V8-9)"
    )


def test_every_column_the_participant_names_exists_in_the_schema() -> None:
    """Catches the ordinary drift: a column renamed in the DDL and missed in one statement."""
    referenced: set[str] = set()
    for statement in _PARTICIPANT_SQL:
        words = set(re.findall(r"[a-z][a-z_]{2,}", statement.lower()))
        referenced |= {w for w in words if w not in _NOT_COLUMNS and not w.startswith(":")}

    # Bound parameters appear as `:subject_ref`; the column of the same name is what we
    # actually want to check, so they collapse together harmlessly.
    missing = sorted(column for column in referenced if column not in _DDL)
    assert not missing, f"handler.py references {missing}, absent from the schema"


def test_the_delete_order_is_the_reverse_of_the_create_order() -> None:
    """Children are created last and deleted first. If those two ever disagree, one of
    them is wrong and the database will say so only at runtime."""
    create_order = re.findall(r"create table if not exists public\.([a-z_]+)", _DDL)
    delete_order = [table.split(".")[-1] for table in handler._DELETE_ORDER]

    referenced = [table for table in create_order if table in delete_order]
    assert delete_order == list(reversed(referenced))


def test_foreign_keys_restrict_rather_than_cascade() -> None:
    """The archetype's whole lesson depends on this.

    `ON DELETE CASCADE` would make `DELETE FROM public.customers` quietly remove the
    invoices and lines as well. It would work, `_DELETE_ORDER` would become decorative,
    and a reader would learn the opposite of "referential integrity dictates ordering".
    RESTRICT makes the database refuse a wrong-order delete, which is what makes the
    ordering demonstrable instead of merely asserted.
    """
    assert "on delete cascade" not in _DDL
    assert _DDL.count("on delete restrict") == 2, "invoices → customers, lines → invoices"


def test_the_fk_parent_map_is_the_schemas_own_references() -> None:
    """`_FK_PARENTS` decides what a legal hold drags into retention with it (V12-3), so a
    hand-maintained copy of the schema's `REFERENCES` clauses is exactly the kind of
    second source of truth that goes stale silently — and the failure mode is a refused
    `DELETE` in phase 3, where nothing compensates.

    Derived from the DDL here, so adding a foreign key or renaming a table turns this red
    rather than turning a deployed erasure red.
    """
    declared: dict[str, tuple[str, ...]] = {}
    for statement in SCHEMA_STATEMENTS:
        created = re.search(r"create table if not exists public\.([a-z_]+)", statement.lower())
        if created is None:
            continue
        parents = tuple(
            f"public.{name}"
            for name in re.findall(r"references\s+public\.([a-z_]+)", statement.lower())
        )
        if parents:
            declared[f"public.{created.group(1)}"] = parents

    assert declared == handler._FK_PARENTS


def test_the_schema_is_idempotent() -> None:
    """`make seed` is re-run constantly (V8-8); applying the schema must converge too."""
    for statement in SCHEMA_STATEMENTS:
        assert "if not exists" in statement.lower(), statement.strip().split("\n")[0]


def test_ensure_schema_waits_out_a_resuming_cluster() -> None:
    """It is usually the first statement a fresh cluster ever sees, so it meets the
    auto-pause resume before anything else does (V8-7)."""
    from botocore.exceptions import ClientError

    class _ResumingOnce:
        def __init__(self) -> None:
            self.calls = 0
            self.resumed = False

        def execute_statement(self, **_: object) -> dict[str, object]:
            self.calls += 1
            if not self.resumed:
                self.resumed = True
                raise ClientError(
                    {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}},
                    "ExecuteStatement",
                )
            return {}

    client = _ResumingOnce()
    applied = ensure_schema(client, cluster_arn="arn:c", secret_arn="arn:s", database="billing")

    assert applied == SCHEMA_STATEMENTS
    assert client.calls == len(SCHEMA_STATEMENTS) + 1, "one retry, then every statement"


@pytest.mark.parametrize("statement", SCHEMA_STATEMENTS, ids=lambda s: s.strip()[:40])
def test_no_schema_statement_carries_subject_data(statement: str) -> None:
    """DDL only. A seeded row inside the schema would put fabricated PII on a code path
    that runs against every environment, including one where it was never intended."""
    lowered = statement.lower()
    assert "insert" not in lowered
    assert "@" not in statement


# ─── the analytics lake's Iceberg DDL (V8-10) ─────────────────────────────────────────


def test_the_iceberg_ddl_matches_athenas_documented_grammar() -> None:
    """Two clauses Athena rejects, each with an unhelpful error.

    `IF NOT EXISTS` routes the statement into the generic CREATE TABLE parser, which wants
    `WITH (...)` and rejects `LOCATION`. `EXTERNAL` fails with "External keyword not
    supported for table type ICEBERG". Both are parse-time failures against a deployed
    workgroup, which is the slowest place to discover a syntax question.
    """
    from pii_erasure.participants.analytics_lake.schema import create_table_sql

    ddl = create_table_sql(database="lake", table="events", location="s3://bucket/events/")

    assert "IF NOT EXISTS" not in ddl
    assert "CREATE EXTERNAL TABLE" not in ddl
    assert ddl.startswith("CREATE TABLE lake.events (")
    # LOCATION then TBLPROPERTIES, in that order — the Iceberg form, not the Trino WITH form.
    assert ddl.index("LOCATION") < ddl.index("TBLPROPERTIES")
    assert "WITH (" not in ddl
    assert "'table_type' = 'ICEBERG'" in ddl


def test_an_existing_table_is_not_an_error() -> None:
    """Idempotency without IF NOT EXISTS: the grammar forbids the clause, so the error is
    caught instead. `make seed` is re-run constantly (V8-8)."""
    from pii_erasure.participants.analytics_lake.schema import ensure_table

    def _already_there(_: str) -> None:
        raise RuntimeError("FAILED: Table already exists: events")

    ensure_table(_already_there, database="lake", table="events", location="s3://b/e/")


def test_a_real_ddl_failure_still_propagates() -> None:
    """The catch must be narrow, or a genuine syntax error becomes a silent no-op and the
    table never exists — which is V8-9 again, this time self-inflicted."""
    from pii_erasure.participants.analytics_lake.schema import ensure_table

    def _syntax_error(_: str) -> None:
        raise RuntimeError("FAILED: mismatched input 'LOCATION'. Expecting: 'WITH'")

    with pytest.raises(RuntimeError, match="mismatched input"):
        ensure_table(_syntax_error, database="lake", table="events", location="s3://b/e/")
