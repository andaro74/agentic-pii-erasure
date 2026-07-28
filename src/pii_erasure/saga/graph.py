"""StateGraph assembly — nodes, edges, `compile(checkpointer=…)`.

`build_graph` is pure wiring over injected dependencies so the hermetic tests drive
the *same* graph with fakes and `InMemorySaver`; `production_graph` binds the real
clients and the DynamoDB checkpointer, once per Lambda container.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from pii_erasure.saga.checkpointer import build_checkpointer
from pii_erasure.saga.compensate import make_compensate
from pii_erasure.saga.deps import SagaDeps, production_deps
from pii_erasure.saga.edges import PATH_MAPS, ROUTERS
from pii_erasure.saga.nodes.approval_gate import make_approval_gate
from pii_erasure.saga.nodes.grace_window import make_grace_window
from pii_erasure.saga.nodes.hard_delete import make_hard_delete
from pii_erasure.saga.nodes.hold_check import make_hold_check
from pii_erasure.saga.nodes.hold_recheck import make_hold_recheck
from pii_erasure.saga.nodes.intake import make_intake
from pii_erasure.saga.nodes.plan import make_plan
from pii_erasure.saga.nodes.soft_delete import make_soft_delete
from pii_erasure.saga.nodes.sweep import make_sweep
from pii_erasure.saga.nodes.verify import make_verify
from pii_erasure.saga.state import SagaState


def build_graph(deps: SagaDeps, checkpointer: Any) -> CompiledStateGraph[SagaState, Any, Any, Any]:
    """The three-phase saga, compiled with a checkpointer. Always with one — a saga
    without durable state is not a smaller saga, it is a different (wrong) program."""
    if checkpointer is None:
        raise ValueError("the saga is never compiled without a checkpointer (ADR-016)")

    builder = StateGraph(SagaState)
    builder.add_node("intake", make_intake(deps))
    builder.add_node("plan", make_plan(deps))
    builder.add_node("hold_check", make_hold_check(deps))
    builder.add_node("soft_delete", make_soft_delete(deps))
    builder.add_node("approval_gate", make_approval_gate(deps))
    builder.add_node("grace_window", make_grace_window(deps))
    builder.add_node("hold_recheck", make_hold_recheck(deps))
    builder.add_node("hard_delete", make_hard_delete(deps))
    builder.add_node("verify", make_verify(deps))
    builder.add_node("sweep", make_sweep(deps))
    builder.add_node("compensate", make_compensate(deps))

    builder.add_edge(START, "intake")
    for node, path_map in PATH_MAPS.items():
        builder.add_conditional_edges(node, ROUTERS[node], path_map)
    builder.add_edge("sweep", END)
    builder.add_edge("compensate", END)

    return builder.compile(checkpointer=checkpointer)


_PRODUCTION_GRAPH: CompiledStateGraph[SagaState, Any, Any, Any] | None = None


def production_graph() -> CompiledStateGraph[SagaState, Any, Any, Any]:
    """Container-lifetime singleton: deps and checkpointer are stateless clients, and
    rebuilding the graph per invocation would only re-run wiring."""
    global _PRODUCTION_GRAPH
    if _PRODUCTION_GRAPH is None:
        _PRODUCTION_GRAPH = build_graph(production_deps(), build_checkpointer())
    return _PRODUCTION_GRAPH
