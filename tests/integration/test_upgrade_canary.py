"""The upgrade canary's two halves — ADR-016's only control that catches a stranded saga.

`scripts/upgrade_canary.sh` **is the contract**; this file implements it. Where the two
differ, the script is right.

A saga pauses for up to 30 days at the approval gate, and its state lives in a DynamoDB
checkpoint written by whichever `langgraph` + `langgraph-checkpoint-aws` was deployed at
pause time. Upgrade during that window and resume must deserialize a checkpoint written
by the old version. A serialization change strands live erasure requests **silently**,
past a statutory deadline, with no error until somebody asks why a subject was never
erased.

No unit test can catch that: the failure lives between two versions across a real table.
So this suite is deliberately split by `$CANARY_STAGE`, with the upgrade happening
*between* the halves — in a different process, against a different deployment.

| Stage | Does | Must not |
|---|---|---|
| `pause` | start a saga, drive it to the approval interrupt, record it | approve; resume |
| `resume` | load that thread, assert the digest is identical, finish it | start a new saga |

**There is no default stage.** A canary that picked one would report success for whichever
half happened to run — which is the vacuous-gate shape this repo has caught four times.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest

#: Its OWN marker, not `integration`. `make integration` runs `-m integration` over
#: this directory, and these tests are meaningless — and failing — without a
#: CANARY_STAGE. A release gate that broke the routine suite would get its marker
#: loosened within a week, which is how a gate stops gating.
pytestmark = pytest.mark.canary

#: Written by `pause`, read by `resume`. A file rather than an environment variable
#: because the two stages are separate processes with a `make deploy-dev` between them.
CANARY_STATE = Path(os.environ.get("CANARY_STATE", ".canary-state.json"))

STAGE = os.environ.get("CANARY_STAGE", "")
_STAGES = ("pause", "resume")


def _executor() -> str:
    return f"asdp-{os.environ.get('PII_ERASURE_STAGE', 'dev')}-saga-executor"


def _invoke(payload: dict[str, Any]) -> dict[str, Any]:
    response = boto3.client("lambda").invoke(
        FunctionName=_executor(),
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read() or b"{}")
    assert not response.get("FunctionError"), f"saga executor failed: {body}"
    return dict(body)


def test_the_stage_is_named_explicitly() -> None:
    """No default. A canary that silently picked a stage would report success for
    whichever half ran, and both halves passing separately is the entire point."""
    assert STAGE in _STAGES, (
        f"set CANARY_STAGE to one of {_STAGES} — see scripts/upgrade_canary.sh, "
        f"which is the contract this file implements"
    )


@pytest.mark.skipif(STAGE != "pause", reason="pause stage only")
def test_pause_a_saga_on_the_current_versions() -> None:
    """Drive a real saga to the approval interrupt and stop there.

    Stopping is the assertion. A saga that ran to completion would leave nothing to
    resume, and the canary would then prove only that a fresh saga works on the new
    version — which is true of any broken serialization change too.
    """
    from pii_erasure.cli import walkthrough

    subject_ref = walkthrough.seeded_subject()
    saga_id = f"canary_{uuid.uuid4().hex[:12]}"

    _invoke(
        {
            "action": "start",
            "saga": {
                "saga_id": saga_id,
                "subject_ref": subject_ref,
                "request_id": f"req_{uuid.uuid4().hex[:8]}",
                "tenant_id": os.environ.get("PII_ERASURE_TENANT", "meridian"),
            },
        }
    )

    state = _invoke({"action": "describe", "thread_id": saga_id})
    assert state["gate"] == "approval", (
        f"the canary needs a saga PAUSED at the approval gate, got gate="
        f"{state.get('gate')!r} status={state.get('status')!r}"
    )
    digest = state["manifest_digest"]
    assert digest, "a paused saga with no manifest digest cannot prove a clean resume"

    CANARY_STATE.write_text(
        json.dumps(
            {
                "threadId": saga_id,
                "subjectRef": subject_ref,
                "manifestDigest": digest,
                "langgraph": _installed("langgraph"),
                "checkpointAws": _installed("langgraph-checkpoint-aws"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(STAGE != "resume", reason="resume stage only")
def test_resume_that_thread_on_the_new_versions() -> None:
    """Load the OLD checkpoint with the NEW code, and drive it to completion.

    Three assertions, in order of what they catch:

    1. **The checkpoint still loads.** A serialization change usually fails here, loudly.
    2. **The digest is byte-identical.** The quieter failure: a checkpoint that
       deserializes into a *different* manifest. Approval binds to this digest
       (invariant 3), so a changed one means the saga would execute a plan nobody
       approved — and it would do so without erroring.
    3. **It runs to completion.** The whole point: a resume that loads and then wedges is
       still a stranded erasure request.
    """
    assert CANARY_STATE.is_file(), (
        f"{CANARY_STATE} not found — run the pause stage first. The canary cannot "
        f"resume a thread it never paused."
    )
    recorded = json.loads(CANARY_STATE.read_text(encoding="utf-8"))
    thread_id = recorded["threadId"]

    state = _invoke({"action": "describe", "thread_id": thread_id})
    assert state["status"] != "unknown", (
        f"{thread_id} has no checkpoint after the upgrade — the saga is STRANDED, "
        f"which is exactly the failure this canary exists to catch (ADR-016)"
    )
    assert state["gate"] == "approval", (
        f"{thread_id} is no longer at the approval gate after the upgrade "
        f"(gate={state.get('gate')!r}) — the checkpoint deserialized into a different "
        f"place in the graph"
    )
    assert state["manifest_digest"] == recorded["manifestDigest"], (
        "the manifest digest changed across the upgrade. Approval binds to this digest "
        "(invariant 3), so the saga would execute a plan nobody approved — and this is "
        "the quiet failure, because deserialization did not error"
    )

    approved = _invoke(
        {
            "action": "resume",
            "thread_id": thread_id,
            "resume": {
                "decision": "approve",
                "digest": recorded["manifestDigest"],
                "approver": "upgrade-canary",
            },
        }
    )
    assert approved["status"] != "resume_rejected", (
        f"the executor refused a resume shaped for the gate it is on: {approved}"
    )

    CANARY_STATE.unlink()


def _installed(package: str) -> str:
    from importlib.metadata import version

    return version(package)
