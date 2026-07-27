"""Conditional routing between phases.

The path maps are module-level DATA, not just closures, because a test asserts the
phase-3 rows never route to `compensate` (invariant 6). Keeping them declarative makes
"restore is unreachable from phase 3" a property you can read and assert, not infer.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END

from pii_erasure.saga.compensate import (
    STATUS_COMPENSATION_FAILED,  # noqa: F401  (re-export for tests)
)
from pii_erasure.saga.nodes.approval_gate import (
    STATUS_APPROVAL_DENIED,
    STATUS_APPROVAL_INVALID,
)
from pii_erasure.saga.nodes.grace_window import STATUS_REVOKED
from pii_erasure.saga.nodes.hard_delete import pending_hard_deletes
from pii_erasure.saga.nodes.soft_delete import STATUS_PHASE2_FAILED
from pii_erasure.saga.state import (
    STATUS_ABORTED,
    STATUS_ALREADY_TOMBSTONED,
    STATUS_BLOCKED,
    STATUS_STUCK,
)

#: Nodes from which no path may reach compensation (invariant 6).
PHASE3_NODES: tuple[str, ...] = ("hard_delete", "verify", "sweep")

#: Every conditional route in the graph. Labels are semantic, targets are node names.
PATH_MAPS: dict[str, dict[str, Any]] = {
    "intake": {"continue": "plan", "halt": END},
    "hold_check": {"continue": "soft_delete", "blocked": END},
    "soft_delete": {"continue": "approval_gate", "failed": "compensate"},
    "approval_gate": {"approved": "grace_window", "unwound": "compensate"},
    "grace_window": {"continue": "hold_recheck", "revoked": "compensate"},
    "hold_recheck": {"continue": "hard_delete", "blocked": END},
    # Phase 3: forward only, one participant per superstep. "next" loops back so
    # every receipt is individually checkpointed; "halt" is the digest-mismatch
    # abort. A stuck participant is a PAUSE inside the node (DLQ + interrupt at the
    # stuck gate), not an edge — and never, under any label, a rollback.
    "hard_delete": {"next": "hard_delete", "continue": "verify", "halt": END},
    "verify": {"continue": "sweep", "halt": END},
}


def route_after_intake(state: dict[str, Any]) -> str:
    return "halt" if state.get("status") == STATUS_ALREADY_TOMBSTONED else "continue"


def route_after_hold_check(state: dict[str, Any]) -> str:
    return "blocked" if state.get("status") == STATUS_BLOCKED else "continue"


def route_after_soft_delete(state: dict[str, Any]) -> str:
    return "failed" if state.get("status") == STATUS_PHASE2_FAILED else "continue"


def route_after_approval_gate(state: dict[str, Any]) -> str:
    if state.get("status") in (STATUS_APPROVAL_DENIED, STATUS_APPROVAL_INVALID):
        return "unwound"
    return "approved"


def route_after_grace_window(state: dict[str, Any]) -> str:
    return "revoked" if state.get("status") == STATUS_REVOKED else "continue"


def route_after_hold_recheck(state: dict[str, Any]) -> str:
    return "blocked" if state.get("status") == STATUS_BLOCKED else "continue"


def route_after_hard_delete(state: dict[str, Any]) -> str:
    if state.get("status") == STATUS_ABORTED:
        return "halt"
    if pending_hard_deletes(state) or not state.get("tombstoned"):
        return "next"
    return "continue"


def route_after_verify(state: dict[str, Any]) -> str:
    return "halt" if state.get("status") == STATUS_STUCK else "continue"


ROUTERS = {
    "intake": route_after_intake,
    "hold_check": route_after_hold_check,
    "soft_delete": route_after_soft_delete,
    "approval_gate": route_after_approval_gate,
    "grace_window": route_after_grace_window,
    "hold_recheck": route_after_hold_recheck,
    "hard_delete": route_after_hard_delete,
    "verify": route_after_verify,
}
