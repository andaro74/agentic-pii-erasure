"""AgentCore Runtime — the discovery subgraph's host, and the only Bedrock caller.

Ships as an **S3 code zip**, not a container image (ADR-025):
`cdk synth` runs inside `make check`, and a `DockerImageAsset` would give the hermetic
gate a Docker-daemon dependency plus arm64 emulation. `entrypoint.py` implements the
HTTP contract on the standard library so it can be started in-process by a test.
"""

from pii_erasure.runtime.entrypoint import (
    PORT,
    SESSION_HEADER,
    DiscoveryHandler,
    build_server,
    discover,
)

__all__ = ["PORT", "SESSION_HEADER", "DiscoveryHandler", "build_server", "discover"]
