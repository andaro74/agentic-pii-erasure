"""Reducer concurrency tests — invariant 10.

Two layers, deliberately: the reducer functions are exercised directly (the unit), and
then through a real two-branch parallel `StateGraph` superstep (the mechanism), because
"the reducer merges correctly when I call it" and "langgraph actually routes concurrent
writes through the reducer" are different claims and the second is the one that
protects recall.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from pii_erasure.saga.state import (
    ReducerConflictError,
    append_unique,
    last_value,
    merge_unique,
    set_once,
)

# ─── merge_unique ─────────────────────────────────────────────────────────────────────


def test_merge_unique_unions_disjoint_keys() -> None:
    merged = merge_unique({"a": 1}, {"b": 2})
    assert merged == {"a": 1, "b": 2}


def test_merge_unique_absorbs_identical_rewrites() -> None:
    merged = merge_unique({"a": {"x": 1}}, {"a": {"x": 1}})
    assert merged == {"a": {"x": 1}}


def test_merge_unique_raises_on_conflict_instead_of_picking_a_winner() -> None:
    with pytest.raises(ReducerConflictError):
        merge_unique({"a": 1}, {"a": 2})


def test_merge_unique_tolerates_none() -> None:
    assert merge_unique(None, {"a": 1}) == {"a": 1}
    assert merge_unique({"a": 1}, None) == {"a": 1}


# ─── append_unique ────────────────────────────────────────────────────────────────────


def test_append_unique_preserves_order_and_dedupes() -> None:
    merged = append_unique(["a", "b"], ["b", "c"])
    assert merged == ["a", "b", "c"]


def test_append_unique_dedupes_dicts_by_equality() -> None:
    merged = append_unique([{"holdId": "h1"}], [{"holdId": "h1"}, {"holdId": "h2"}])
    assert merged == [{"holdId": "h1"}, {"holdId": "h2"}]


# ─── set_once ─────────────────────────────────────────────────────────────────────────


def test_set_once_accepts_first_write_and_identical_rewrite() -> None:
    assert set_once(None, "sha256:abc") == "sha256:abc"
    assert set_once("sha256:abc", "sha256:abc") == "sha256:abc"


def test_set_once_raises_on_overwrite() -> None:
    with pytest.raises(ReducerConflictError):
        set_once("sha256:abc", "sha256:def")


def test_set_once_keeps_current_on_none() -> None:
    assert set_once("kept", None) == "kept"


def test_set_once_treats_channel_default_empties_as_unset() -> None:
    """langgraph initialises binop channels with the type's default (`""`/`{}`), not
    None — the first real write must not read as an overwrite."""
    assert set_once("", "saga_1") == "saga_1"
    assert set_once({}, {"a": 1}) == {"a": 1}


# ─── last_value ───────────────────────────────────────────────────────────────────────


def test_last_value_progresses_and_ignores_none() -> None:
    assert last_value("running", "completed") == "completed"
    assert last_value("running", None) == "running"


# ─── through a real parallel superstep ────────────────────────────────────────────────


class _TwoWriterState(TypedDict, total=False):
    receipts: Annotated[dict[str, Any], merge_unique]
    holds: Annotated[list[dict[str, Any]], append_unique]


def _parallel_graph(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Fan out to two nodes that write concurrently in ONE superstep, then join."""
    builder = StateGraph(_TwoWriterState)
    builder.add_node("left", lambda _state: left)
    builder.add_node("right", lambda _state: right)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", END)
    builder.add_edge("right", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    return graph.invoke({}, {"configurable": {"thread_id": "t"}})


def test_concurrent_receipt_writes_merge_instead_of_overwriting() -> None:
    result = _parallel_graph(
        {"receipts": {"soft_delete:profile-store": {"outcome": "APPLIED"}}},
        {"receipts": {"soft_delete:vector-index": {"outcome": "APPLIED"}}},
    )
    # Last-write-wins would leave exactly one key here — the silent overwrite that
    # surfaces later as a recall failure. Both must survive.
    assert set(result["receipts"]) == {
        "soft_delete:profile-store",
        "soft_delete:vector-index",
    }


def test_concurrent_hold_writes_append_without_loss() -> None:
    result = _parallel_graph(
        {"holds": [{"holdId": "h1"}]},
        {"holds": [{"holdId": "h2"}]},
    )
    assert {h["holdId"] for h in result["holds"]} == {"h1", "h2"}


def test_concurrent_conflicting_writes_fail_loudly_not_silently() -> None:
    with pytest.raises(ReducerConflictError):
        _parallel_graph(
            {"receipts": {"soft_delete:profile-store": {"outcome": "APPLIED"}}},
            {"receipts": {"soft_delete:profile-store": {"outcome": "REFUSED"}}},
        )
