"""Reducer concurrency tests — invariant 10.

Two layers, deliberately: the reducer functions are exercised directly (the unit), and
then through a real two-branch parallel `StateGraph` superstep (the mechanism), because
"the reducer merges correctly when I call it" and "langgraph actually routes concurrent
writes through the reducer" are different claims and the second is the one that
protects recall.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict, get_args, get_type_hints

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from pii_erasure.saga.state import (
    ReducerConflictError,
    SagaState,
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


def test_started_at_is_write_once_rather_than_last_value() -> None:
    """The anchor for both duration metrics in §10.1, so which reducer holds it matters.

    Under `last_value` a re-executed intake would quietly move the start forward and every
    duration would shrink toward zero — an SLO that reports better the more the saga
    retries. `set_once` makes that a loud failure instead, which is why `intake` reads the
    field before writing it.
    """
    annotation = get_type_hints(SagaState, include_extras=True)["started_at"]
    assert set_once in get_args(annotation), "started_at is not reduced by set_once"


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


# ─── adding a state key to a graph with live paused threads ───────────────────────────


class _Before(TypedDict, total=False):
    seen: Annotated[list[str], append_unique]


class _After(TypedDict, total=False):
    seen: Annotated[list[str], append_unique]
    started_at: Annotated[str | None, set_once]


def _gate_graph(schema: Any, saver: InMemorySaver) -> Any:
    """A one-node graph that pauses at an interrupt, so there is a checkpoint to resume."""

    def gate(_state: dict[str, Any]) -> dict[str, Any]:
        answer = interrupt({"gate": "test"})
        return {"seen": [str(answer)]}

    builder = StateGraph(schema)
    builder.add_node("gate", gate)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=saver)


def test_a_checkpoint_written_before_a_field_existed_resumes_on_the_graph_that_has_it() -> None:
    """The compatibility claim `started_at` rests on — checked, not remembered.

    Every saga paused at an approval gate right now was checkpointed by a graph whose
    state schema had no `started_at`. Those threads must resume on the graph that does and
    simply not carry the field. This is invariant 9's failure mode approached from our own
    side rather than the framework's: a resume that cannot deserialize strands a live
    erasure request silently, past a statutory deadline, and if langgraph required the
    channel sets to match then an additive state key would do exactly that.

    A version bump gets `make upgrade-canary`. A state-shape change gets this.
    """
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "schema_change"}}

    paused = _gate_graph(_Before, saver).invoke({}, config, durability="sync")
    assert paused["__interrupt__"], "nothing paused, so the resume below would prove nothing"

    resumed = _gate_graph(_After, saver).invoke(Command(resume="answer"), config, durability="sync")

    assert resumed["seen"] == ["answer"]
    assert "started_at" not in resumed, "the new channel invented a value for an old thread"
