"""The discovery subgraph — a LangGraph `StateGraph`, and invariant 1's assertion point.

A **subgraph, not a free-running agent**, for one reason that shows up in the tests: a
subgraph's tool list is fixed at construction, so it can be asserted. An agent that picks
up tools as it goes can only be observed, and "we watched it and it never called
`hard_delete`" is not the claim this architecture makes. The claim is that it *cannot*,
and :func:`build_discovery_subgraph` raises before returning a graph if the toolset it is
handed carries anything mutating.

Shape (ARCHITECTURE §6.2):

    cartographer → prospector → lineage → counsel → editor

Linear rather than a fan-out at this milestone. The docs describe discovery as "divergent,
parallel fan-out", and the *sweep* inside `prospector` is exactly that — every participant,
every run. What is linear is the reasoning pipeline over the sweep's results, and making
those five nodes concurrent would buy nothing: each one consumes the previous one's output.

**Where the model actually sits.** Three of the five nodes are deterministic Python
(`cartographer`, `prospector`, `editor`); the model contributes scope hints and
annotations, and the graph runs correctly with no model client at all — `model=None` is a
supported configuration, exercised by the unit tests, and the *recall* path never depends
on a model being reachable. That is deliberate: recall 1.0 is a structural property of the
exhaustive sweep (see `agents/prospector.py`), so a model outage degrades depth and
precision, never completeness.

Discovery mutates nothing, so every failure here is fail-closed by construction: the worst
outcome is no manifest, which is a retry rather than a breach.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pii_erasure.contract.tools import READ_ONLY_TOOLS
from pii_erasure.discovery.advisor import Advisor, merge_hints
from pii_erasure.discovery.agents.cartographer import candidate_systems
from pii_erasure.discovery.agents.counsel import evaluate_holds, held_systems
from pii_erasure.discovery.agents.editor import excluded_systems, reconcile
from pii_erasure.discovery.agents.lineage import derived_relationships
from pii_erasure.discovery.agents.prospector import ProbeResult, sweep
from pii_erasure.discovery.tools import GatewayToolset, MutatingToolRefusedError

#: Bumped when the graph's behaviour changes in a way a reader of a manifest would care
#: about. Lands in `provenance.agentVersion`, which IS digested — so a manifest approved
#: under one version cannot be silently executed as though produced by another.
AGENT_VERSION = "discovery-subgraph@m7"


def _merge_probes(
    current: tuple[ProbeResult, ...], incoming: tuple[ProbeResult, ...]
) -> tuple[ProbeResult, ...]:
    """Append-merge, last-writer-wins per system.

    Invariant 10's rule applied one plane over: a plain overwrite here would let a
    second sweep's results replace a first's wholesale, and two participants' findings
    silently overwriting each other surfaces as a *recall failure*, not a crash.
    """
    merged = {result.system_id: result for result in current}
    merged.update({result.system_id: result for result in incoming})
    return tuple(sorted(merged.values(), key=lambda r: r.system_id))


class DiscoveryState(TypedDict, total=False):
    """What flows through the subgraph. Subject-shaped, and therefore never written to
    AgentCore Memory (invariant 13) — the checkpointer is where this may legitimately
    live, and it is a different store for exactly that reason (ADR-019)."""

    subject_ref: str
    saga_id: str
    tenant: str
    priors: tuple[str, ...]
    scope_hints: tuple[str, ...]
    candidates: tuple[str, ...]
    probes: Annotated[tuple[ProbeResult, ...], _merge_probes]
    lineage: tuple[tuple[str, str], ...]
    holds: tuple[dict[str, Any], ...]
    malformed_holds: tuple[dict[str, Any], ...]
    held_systems: tuple[str, ...]
    participants: tuple[dict[str, Any], ...]
    excluded: tuple[str, ...]
    incomplete: tuple[str, ...]


def assert_read_only(toolset: GatewayToolset) -> tuple[str, ...]:
    """The verbatim guard. Returns the verbs iff every one is read-only.

    Kept as its own function so the unit test asserts *the thing the graph calls*,
    not a re-implementation of it that could pass while the graph is broken.
    """
    offending = [verb for verb in toolset.verbs if verb not in READ_ONLY_TOOLS]
    if offending:
        raise MutatingToolRefusedError(offending)
    return tuple(toolset.verbs)


def build_discovery_subgraph(
    toolset: GatewayToolset,
    *,
    advisor: Advisor | None = None,
    checkpointer: Any = None,
) -> Any:
    """Compile the discovery subgraph.

    `advisor` is the model, and it is optional. `None` runs the graph deterministically
    with identical recall — the sweep is exhaustive either way — which is why
    `make check` needs no model and why a Bedrock outage degrades depth rather than
    completeness.

    Raises `MutatingToolRefusedError` before any graph exists if `toolset` carries a
    mutating verb. There is no flag, no debug path, and no test hook that bypasses it
    (CLAUDE.md invariant 1).
    """
    verbs = assert_read_only(toolset)

    def cartographer(state: DiscoveryState) -> dict[str, Any]:
        return {"candidates": candidate_systems(state.get("priors", ()))}

    def prospector(state: DiscoveryState) -> dict[str, Any]:
        candidates = state.get("candidates") or candidate_systems()
        hints = state.get("scope_hints", ())
        if advisor is not None:
            # The model's ONE effect on the sweep, and it can only widen it: a hint
            # tells a participant to look under an additional key. It cannot remove a
            # candidate — `candidates` is already fixed above and is not passed back
            # through the advisor — so a hostile or hallucinated hint costs precision
            # and never recall (ADR-008's asymmetry).
            hints = merge_hints(
                hints,
                advisor.scope_hints(
                    subject_ref=state["subject_ref"],
                    systems=candidates,
                    priors=state.get("priors", ()),
                ),
            )
        report = sweep(
            toolset,
            subject_ref=state["subject_ref"],
            saga_id=state["saga_id"],
            candidates=candidates,
            scope_hints=hints,
        )
        return {
            "probes": report.results,
            "scope_hints": tuple(hints),
            "incomplete": tuple(probe.system_id for probe in report.errors),
        }

    def lineage_tracer(state: DiscoveryState) -> dict[str, Any]:
        return {"lineage": derived_relationships(state.get("probes", ()))}

    def counsel(state: DiscoveryState) -> dict[str, Any]:
        findings, malformed = evaluate_holds(state.get("probes", ()))
        return {
            "holds": tuple(hold.as_contract() for hold in findings),
            "malformed_holds": tuple(
                {"systemId": entry.system_id, "missing": list(entry.missing)} for entry in malformed
            ),
            "held_systems": tuple(sorted(held_systems(findings))),
        }

    def editor(state: DiscoveryState) -> dict[str, Any]:
        probes = state.get("probes", ())
        findings, _malformed = evaluate_holds(probes)
        participants = reconcile(
            probes,
            holds=findings,
            lineage=state.get("lineage", ()),
            annotations=None,
        )
        return {"participants": participants, "excluded": excluded_systems(probes)}

    graph: StateGraph[DiscoveryState, Any, Any, Any] = StateGraph(DiscoveryState)
    graph.add_node("cartographer", cartographer)
    graph.add_node("prospector", prospector)
    graph.add_node("lineage", lineage_tracer)
    graph.add_node("counsel", counsel)
    graph.add_node("editor", editor)

    graph.add_edge(START, "cartographer")
    graph.add_edge("cartographer", "prospector")
    graph.add_edge("prospector", "lineage")
    graph.add_edge("lineage", "counsel")
    graph.add_edge("counsel", "editor")
    graph.add_edge("editor", END)

    compiled = graph.compile(checkpointer=checkpointer)
    # Carried for the assertion tests and for provenance. Reading the tool list off
    # the compiled object is what a reviewer will try first; make it findable.
    compiled.discovery_verbs = verbs
    compiled.agent_version = AGENT_VERSION
    compiled.model_id = advisor.model_id if advisor is not None else None
    return compiled


def discovery_tool_names(verbs: Sequence[str] = ("discover", "verify")) -> tuple[str, ...]:
    """The Cedar/MCP action names this subgraph may ever invoke."""
    from pii_erasure.discovery.tools import expected_tool_surface

    return expected_tool_surface(verbs)
