"""The AgentCore Runtime HTTP contract — `POST /invocations`, `GET /ping`, port 8080.

Runs the discovery subgraph inside AgentCore Runtime: the only compute in this platform
permitted to call Bedrock, and the only place a model runs at all.

**Why stdlib rather than the `bedrock-agentcore` SDK's `@app.entrypoint`** (ADR-025):
the deployment package has a 250 MB zipped ceiling that `langgraph` + `langchain` +
`numpy` already spend two-thirds of, and — the reason that actually decided it — a server
we own can be started **in-process by a unit test**. `/ping` and `/invocations` are
therefore assertable in `make check`, on a laptop, with no AWS account. A contract
delegated to a vendored framework is a contract nobody in this repo can test.

The contract, verified against the AgentCore service-contract docs rather than recalled:

| Path | Method | Contract |
|---|---|---|
| `/invocations` | POST | JSON in, JSON out. Session id on the
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header |
| `/ping` | GET | `{"status": "Healthy"}` — `HealthyBusy` keeps the session alive |

Two rules this module keeps and a reader should check it against:

* **`time_of_last_update` is set only on an actual status change.** The docs are explicit
  that advancing it on every ping signals continuous change, defeats the idle-session
  timeout, and burns session quota until `MaxLifetime`. So it is stamped once, when the
  status transitions, and never on a read.
* **Nothing subject-shaped is logged** (invariant 5). `subjectRef` is a pseudonymous
  handle and is safe to correlate on; artifact locators and error bodies are not, so
  failures are reported by class rather than by content.
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pii_erasure.discovery.subgraph import AGENT_VERSION, build_discovery_subgraph
from pii_erasure.discovery.tools import read_only_toolset
from pii_erasure.observability.redact import scrub

#: Fixed by the AgentCore Runtime contract. Not configurable, and pretending otherwise
#: with an env var would only invite someone to change it and lose an hour.
PORT = 8080
HOST = "0.0.0.0"  # noqa: S104 — the contract requires binding all interfaces

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

_STATUS_HEALTHY = "Healthy"
_STATUS_BUSY = "HealthyBusy"


class _Health:
    """Ping status with an honest `time_of_last_update`.

    Deliberately not a plain string: the field must move only when the status moves,
    and the only reliable way to guarantee that is to make the transition the single
    place it is written.
    """

    def __init__(self) -> None:
        self._status = _STATUS_HEALTHY
        self._changed_at = int(time.time())

    def set(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self._changed_at = int(time.time())

    def payload(self) -> dict[str, Any]:
        return {"status": self._status, "time_of_last_update": self._changed_at}


def discover(
    payload: dict[str, Any],
    *,
    toolset: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run one discovery pass. The pure core, so tests need no HTTP server at all.

    Returns the *candidate manifest body* — participants, holds, exclusions,
    provenance. Not a signed manifest: signing is the saga's `plan` node against the
    real CMK (invariant 2 — the reasoning plane produces a plan, it does not authorise
    one).
    """
    subject_ref = payload.get("subjectRef")
    saga_id = payload.get("sagaId")
    if not subject_ref or not saga_id:
        raise ValueError("subjectRef and sagaId are both required")

    if toolset is None:
        gateway_url = os.environ["ASDP_GATEWAY_URL"]
        region = os.environ.get("AWS_REGION", "us-west-2")
        toolset = read_only_toolset(gateway_url=gateway_url, region=region)

    graph = build_discovery_subgraph(toolset)
    state = graph.invoke(
        {
            "subject_ref": subject_ref,
            "saga_id": saga_id,
            "tenant": payload.get("tenant", "default"),
            "priors": tuple(payload.get("priors") or ()),
            "scope_hints": tuple(payload.get("scopeHints") or ()),
        }
    )

    return {
        "subjectRef": subject_ref,
        "sagaId": saga_id,
        "participants": list(state.get("participants") or ()),
        "legalHolds": list(state.get("holds") or ()),
        "malformedHolds": list(state.get("malformed_holds") or ()),
        "excluded": list(state.get("excluded") or ()),
        "incomplete": list(state.get("incomplete") or ()),
        "provenance": {
            # `discoveredAt` and the session id are volatile and MUST NOT enter a
            # digested body (invariant 4) — the manifest builder drops them into
            # `provenance`, which canonicalisation excludes. Named here so the next
            # reader does not have to rediscover why they are safe.
            "agentVersion": AGENT_VERSION,
            "runtimeSessionId": session_id,
        },
        "toolCalls": [
            {"tool": call.tool, "ok": call.ok, "denied": call.denied}
            for call in getattr(toolset, "calls", [])
        ],
    }


class DiscoveryHandler(BaseHTTPRequestHandler):
    """The two paths the contract defines, and nothing else."""

    server_version = f"asdp-discovery/{AGENT_VERSION}"
    health: _Health = _Health()
    #: Injected by tests; None means "build one from the environment".
    toolset_factory: Any = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # BaseHTTPRequestHandler logs the raw request line to stderr. A query string
        # or an error body could carry subject content, so it goes through the
        # scrubber rather than straight out (invariant 5).
        print(scrub(format % args))

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/ping":
            self._send(200, self.health.payload())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/invocations":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        session_id = self.headers.get(SESSION_HEADER)

        self.health.set(_STATUS_BUSY)
        try:
            payload = json.loads(raw or b"{}")
            toolset = self.toolset_factory() if self.toolset_factory else None
            result = discover(payload, toolset=toolset, session_id=session_id)
        except ValueError as error:
            self._send(400, {"error": "ValidationException", "message": scrub(str(error))})
            return
        except Exception as error:
            # Class name only. An exception message here can carry an artifact locator
            # or a participant payload, and this response leaves the microVM.
            self._send(500, {"error": type(error).__name__})
            return
        finally:
            self.health.set(_STATUS_HEALTHY)
        self._send(200, result)


def build_server(*, port: int = PORT, toolset_factory: Any = None) -> ThreadingHTTPServer:
    """Construct the server without starting it — the seam the unit tests use."""
    handler = type(
        "BoundDiscoveryHandler",
        (DiscoveryHandler,),
        {"toolset_factory": staticmethod(toolset_factory) if toolset_factory else None},
    )
    return ThreadingHTTPServer((HOST, port), handler)


def main() -> None:
    server = build_server()
    print(f"asdp discovery runtime listening on {HOST}:{PORT} ({AGENT_VERSION})")
    server.serve_forever()


if __name__ == "__main__":
    main()
