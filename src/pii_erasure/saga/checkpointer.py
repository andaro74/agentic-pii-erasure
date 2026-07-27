"""The checkpointer — the system of record (ADR-016). `DynamoDBSaver` + S3 offload.

Constructor shape verified against the installed langgraph-checkpoint-aws 1.2.0
source: single table with string keys ``PK``/``SK``, TTL attribute ``ttl``, payloads
over 350 KB offloaded to the S3 bucket under ``key_prefix``.

TTL policy: 180 days from each write. The architecture's retention is "life of saga
+ 90d" (§7.2); the longest legitimate single pause is the 30-day approval window or
the 30-day grace window, and the full arc tops out around 90 days with the T+30
sweep — so 180 days from the *last* write always outlives the saga by more than the
required margin, without keeping dead threads forever. Setting `ttl_seconds` also
makes the saver manage the offload bucket's lifecycle rule at construction time —
the execution role carries the two lifecycle permissions for exactly that call.
"""

from __future__ import annotations

import os

from langgraph_checkpoint_aws import DynamoDBSaver

CHECKPOINT_TTL_SECONDS = 180 * 24 * 60 * 60


def build_checkpointer(
    *,
    table_name: str | None = None,
    offload_bucket: str | None = None,
) -> DynamoDBSaver:
    """The production saver, from the Lambda environment. Loud on missing config —
    a saga whose checkpoints go nowhere must not start (ADR-016)."""
    table = table_name or os.environ["CHECKPOINTS_TABLE"]
    bucket = offload_bucket or os.environ["CHECKPOINT_OFFLOAD_BUCKET"]
    return DynamoDBSaver(
        table_name=table,
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
        s3_offload_config={"bucket_name": bucket, "key_prefix": "checkpoints"},
    )
