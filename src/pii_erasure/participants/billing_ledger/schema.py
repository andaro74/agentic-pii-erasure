"""The billing ledger's schema, next to the SQL that depends on it.

`handler.py` names `public.customers`, `public.invoices`, `public.invoice_lines` and
`public.legal_holds` in fixed statements. Nothing created them (V8-9), so `make seed`
failed with `relation "public.customers" does not exist` — the first statement the seeder
ran was the first thing that had ever needed the tables to be real.

The DDL lives **here rather than in `infra/`** because CloudFormation cannot create tables
inside a database; it would take a custom resource running this SQL anyway. Keeping it
beside the queries means the table names, the column names and the delete ordering are all
in one file, and a unit test asserts the handler never references something the schema does
not define.

**The foreign keys are `ON DELETE RESTRICT`, and that is load-bearing.** `ON DELETE
CASCADE` would make `DELETE FROM public.customers` silently remove the invoices and lines
too — which would work, and would delete the entire point of the RELATIONAL archetype.
Referential integrity is supposed to *dictate ordering*; with CASCADE there is no ordering
to get right, the participant's `_DELETE_ORDER` becomes decorative, and a reader would
learn the opposite of the lesson. RESTRICT makes the database refuse a wrong-order delete,
which is what makes the ordering demonstrable rather than asserted.

Every statement is `IF NOT EXISTS`: `make seed` is re-run constantly (V8-8), and applying
the schema must converge like everything else.
"""

from __future__ import annotations

from typing import Any

#: Applied in order. Parents before children, because the foreign keys require it — the
#: mirror image of `handler._DELETE_ORDER`, which goes children first.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS public.customers (
        subject_ref  TEXT PRIMARY KEY,
        display_name TEXT,
        email        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public.invoices (
        invoice_id     TEXT PRIMARY KEY,
        subject_ref    TEXT NOT NULL
                       REFERENCES public.customers (subject_ref) ON DELETE RESTRICT,
        amount_cents   BIGINT,
        pending_delete BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public.invoice_lines (
        line_id     TEXT PRIMARY KEY,
        invoice_id  TEXT NOT NULL
                    REFERENCES public.invoices (invoice_id) ON DELETE RESTRICT,
        description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public.legal_holds (
        hold_id     TEXT PRIMARY KEY,
        subject_ref TEXT NOT NULL,
        authority   TEXT NOT NULL,
        scope       TEXT NOT NULL,
        basis       TEXT NOT NULL,
        expires_at  DATE
    )
    """,
    # Discovery counts by subject on every table. Without these the seeded set is small
    # enough not to care, but the query plan a reader sees would not be the one a real
    # ledger uses.
    "CREATE INDEX IF NOT EXISTS invoices_subject_ref_idx ON public.invoices (subject_ref)",
    "CREATE INDEX IF NOT EXISTS legal_holds_subject_ref_idx ON public.legal_holds (subject_ref)",
)


def ensure_schema(
    client: Any, *, cluster_arn: str, secret_arn: str, database: str
) -> tuple[str, ...]:
    """Apply the schema idempotently. Returns the statements executed.

    Uses `execute_with_resume`, because this is typically the very first statement sent to
    a freshly deployed cluster and will meet the auto-pause resume (V8-7).
    """
    from pii_erasure.participants.billing_ledger.handler import execute_with_resume

    for statement in SCHEMA_STATEMENTS:
        execute_with_resume(
            client,
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=statement,
        )
    return SCHEMA_STATEMENTS
