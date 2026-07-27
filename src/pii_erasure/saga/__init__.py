"""The three-phase saga (ARCHITECTURE §5) — deterministic replay of an approved manifest.

The system of record. The graph is compiled with `DynamoDBSaver`; the checkpointer *is*
the saga's state, and nothing may hold saga state anywhere else (ADR-016). Nodes are
plain Python — no model client, ever (invariant 2, enforced by test and by IAM).
"""
