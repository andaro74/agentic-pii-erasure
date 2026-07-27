"""The AgentCore Runtime HTTP contract, exercised against a real server, hermetically.

This file is [ADR-025](../../docs/adr/ADR-025-runtime-ships-a-code-zip.md)'s dividend.
The entrypoint implements `/invocations` and `/ping` on the standard library rather than
delegating to `@app.entrypoint` from the `bedrock-agentcore` SDK, so the contract can be
started **in-process, on a laptop, with no AWS account** and asserted for real: a socket,
an HTTP request, a parsed response.

The alternative would have been a contract nobody in this repo can test — the deploy
would be the first thing that ever exercised it, which is the situation V10-1 through
V10-4 came out of.

Contract facts under test, read from the AgentCore service-contract docs rather than
recalled:

* `GET /ping` → 200, `{"status": "Healthy"}`
* `POST /invocations` → 200, JSON; session id arrives on
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`
* `time_of_last_update` moves **only on an actual status change** — the docs warn that
  advancing it every ping defeats the idle-session timeout and burns session quota
* a bad request is a 400, not a 500, and neither leaks a traceback
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from pii_erasure.contract.registry import system_ids
from pii_erasure.discovery.tools import GatewayError, GatewayToolset
from pii_erasure.runtime.entrypoint import SESSION_HEADER, build_server, discover

GATEWAY = "https://gw.example.invalid/mcp"


class ScriptedToolset(GatewayToolset):
    """Canned participant responses. No network, no credentials, no AWS."""

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        surface: tuple[str, ...] = ("profile-store___discover", "profile-store___verify"),
    ) -> None:
        super().__init__(
            gateway_url=GATEWAY, region="us-west-2", verbs=("discover", "verify"), session=object()
        )
        self._responses = responses or {}
        self._surface = surface

    def list_tools(self) -> tuple[str, ...]:
        if self._surface is None:
            raise GatewayError("tools/list refused")
        return self._surface

    def call(self, system_id: str, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(self._responses.get(system_id, {"found": False, "artifacts": []}))


@pytest.fixture
def server_url() -> Iterator[str]:
    """A real HTTP server on an ephemeral port, torn down however the test exits."""
    responses = {
        "profile-store": {
            "found": True,
            "artifacts": [{"kind": "row", "locator": "profile-store:sub_probe"}],
        }
    }
    server = build_server(port=0, toolset_factory=lambda: ScriptedToolset(responses))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(url: str, body: dict[str, Any], **headers: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _get(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


# ─── /ping ────────────────────────────────────────────────────────────────────────────


def test_ping_reports_healthy(server_url: str) -> None:
    status, body = _get(f"{server_url}/ping")
    assert status == 200
    assert body["status"] == "Healthy"


def test_ping_does_not_advance_its_timestamp_on_every_read(server_url: str) -> None:
    """The documented footgun: a `time_of_last_update` that moves on every ping signals
    continuous status change, so the idle-session timeout never fires and sessions
    persist to MaxLifetime, exhausting the session quota. Only a real transition
    should move it."""
    first = _get(f"{server_url}/ping")[1]["time_of_last_update"]
    second = _get(f"{server_url}/ping")[1]["time_of_last_update"]
    assert first == second


# ─── /invocations ─────────────────────────────────────────────────────────────────────


def test_invocations_returns_a_candidate_manifest_body(server_url: str) -> None:
    status, body = _post(
        f"{server_url}/invocations", {"subjectRef": "sub_probe", "sagaId": "saga_probe"}
    )
    assert status == 200
    assert body["subjectRef"] == "sub_probe"
    assert [p["systemId"] for p in body["participants"]] == ["profile-store"]
    # Every other registered participant was probed and reported nothing — named
    # explicitly, because silence is not an exclusion (§11.3 manifest_completeness).
    assert set(body["excluded"]) == set(system_ids()) - {"profile-store"}
    assert body["incomplete"] == []


def test_the_session_id_header_reaches_provenance(server_url: str) -> None:
    _status, body = _post(
        f"{server_url}/invocations",
        {"subjectRef": "sub_probe", "sagaId": "saga_probe"},
        **{SESSION_HEADER: "sess-abc123"},
    )
    assert body["provenance"]["runtimeSessionId"] == "sess-abc123"


def test_the_agent_version_is_reported(server_url: str) -> None:
    """`agentVersion` IS digested (invariant 4), so a manifest approved under one
    version cannot be silently executed as though produced by another."""
    _status, body = _post(f"{server_url}/invocations", {"subjectRef": "s", "sagaId": "g"})
    assert body["provenance"]["agentVersion"].startswith("discovery-subgraph@")


@pytest.mark.parametrize(
    "payload",
    [{}, {"subjectRef": "s"}, {"sagaId": "g"}],
    ids=["empty", "no-saga", "no-subject"],
)
def test_a_malformed_request_is_a_400(server_url: str, payload: dict[str, Any]) -> None:
    status, body = _post(f"{server_url}/invocations", payload)
    assert status == 400
    assert body["error"] == "ValidationException"


def test_unknown_paths_are_404(server_url: str) -> None:
    assert _get(f"{server_url}/nope")[0] == 404
    assert _post(f"{server_url}/invoke", {})[0] == 404


def test_an_internal_failure_reports_a_class_not_a_traceback() -> None:
    """This response leaves the microVM. An exception message here can carry an
    artifact locator or a participant payload (invariant 5), so only the class name
    crosses the boundary."""

    class Exploding(ScriptedToolset):
        def call(self, system_id: str, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("locator profile-store:sub_a3f9 blew up")

    server = build_server(port=0, toolset_factory=Exploding)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = _post(f"{url}/invocations", {"subjectRef": "s", "sagaId": "g"})
        assert status == 500
        assert set(body) == {"error"}
        assert "sub_a3f9" not in json.dumps(body)
        assert "Traceback" not in json.dumps(body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ─── the pure core, without a socket ─────────────────────────────────────────────────


def test_discover_is_callable_without_an_http_server() -> None:
    """The seam that keeps the eval harness from needing a server."""
    result = discover(
        {"subjectRef": "sub_x", "sagaId": "saga_x"},
        toolset=ScriptedToolset({"vector-index": {"found": True, "artifacts": [{"k": "v"}]}}),
    )
    assert [p["systemId"] for p in result["participants"]] == ["vector-index"]


def test_discover_records_the_tool_trajectory() -> None:
    """`no_premature_hard_delete` and the adversarial suite assert on what was called,
    so the trajectory has to be observable rather than inferred."""
    result = discover({"subjectRef": "s", "sagaId": "g"}, toolset=ScriptedToolset())
    assert result["toolCalls"] == []  # ScriptedToolset bypasses the recording layer
    assert result["incomplete"] == []


# ─── the tool surface is reported BY the identity (V10-8) ────────────────────────────


def test_the_runtime_reports_its_own_tool_surface() -> None:
    """`tool_surface_minimality` is measured here because the Runtime *is* the
    discovery identity. The harness used to assume `asdp-{stage}-discovery` and call
    `tools/list` itself — which fails, correctly: that role trusts only
    `bedrock-agentcore.amazonaws.com`, and adding a human to its trust policy would
    weaken the boundary the measurement exists to check."""
    result = discover({"subjectRef": "s", "sagaId": "g"}, toolset=ScriptedToolset())
    assert result["toolSurface"] == ["profile-store___discover", "profile-store___verify"]


def test_a_failed_listing_yields_an_empty_surface_that_fails_the_evaluator() -> None:
    """The one outcome worse than an error is a vacuous pass.

    An empty surface must not read as "no mutating tools, therefore safe". It does not:
    `tool_surface_minimality` compares sets, so empty != expected and the verdict is a
    failure. This test pins both halves — the empty surface, and that it fails.
    """
    from evals.evaluators import tool_surface_minimality

    result = discover({"subjectRef": "s", "sagaId": "g"}, toolset=ScriptedToolset(surface=None))
    assert result["toolSurface"] == []
    verdict = tool_surface_minimality(
        observed=result["toolSurface"], expected=["profile-store___discover"]
    )
    assert not verdict.passed, "an unmeasurable surface passed as though it were minimal"


def test_a_failed_listing_does_not_fail_the_discovery_run() -> None:
    """The surface is evidence, not control flow. Losing it must not lose the manifest —
    recall is the safety-critical metric and it does not depend on this."""
    result = discover(
        {"subjectRef": "s", "sagaId": "g"},
        toolset=ScriptedToolset(
            {"profile-store": {"found": True, "artifacts": [{"kind": "row"}]}}, surface=None
        ),
    )
    assert [p["systemId"] for p in result["participants"]] == ["profile-store"]
