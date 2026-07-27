"""Phase 1 — the reasoning plane, and the only place in this system a model runs.

Read-only by construction. The subgraph is handed a toolset carrying exactly `discover`
and `verify`, and `build_discovery_subgraph` refuses to compile if it is handed anything
else (invariant 1). Discovery output is a *candidate* manifest: it mutates nothing, so
every failure mode here is fail-closed.

The reasoning plane is deliberately the **least privileged** plane in the platform. It
holds no participant IAM, no Bedrock permission beyond model invocation, and no route to
subject data except a SigV4-signed MCP call to the Gateway where Cedar decides. Discovery
reads subject-controlled content — profile bios, object metadata — and is therefore
injection-reachable by design. Its lack of privilege is the entire security claim.
"""

from pii_erasure.discovery.memory import (
    MemoryWriteRejectedError,
    Prior,
    TopologyMemory,
    assert_topology_only,
    ordered_candidates,
)
from pii_erasure.discovery.subgraph import (
    AGENT_VERSION,
    DiscoveryState,
    assert_read_only,
    build_discovery_subgraph,
    discovery_tool_names,
)
from pii_erasure.discovery.tools import (
    GatewayError,
    GatewayToolset,
    MutatingToolRefusedError,
    expected_tool_surface,
    read_only_toolset,
)

__all__ = [
    "AGENT_VERSION",
    "DiscoveryState",
    "GatewayError",
    "GatewayToolset",
    "MemoryWriteRejectedError",
    "MutatingToolRefusedError",
    "Prior",
    "TopologyMemory",
    "assert_read_only",
    "assert_topology_only",
    "build_discovery_subgraph",
    "discovery_tool_names",
    "expected_tool_surface",
    "ordered_candidates",
    "read_only_toolset",
]
