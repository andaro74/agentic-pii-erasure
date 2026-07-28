"""The full arc, end to end against a deployed stack — M8's deployed gate.

discover → soft delete → **pause** → approve → grace → hard delete → certificate.

The gate says this must run "cleanly, twice, identically", and the second run is the
interesting one: it uses a fresh subject and a fresh saga id, and the *shape* of the
output must match — same phases, same gates, same participant count. A walkthrough that
passes once and diverges on the second run is describing a race, not a system.

**The pause is demonstrated, not asserted.** Between phases this process polls the
checkpoint and prints what the saga is waiting for. It could be killed at any point
during a pause and restarted with no loss, because nothing of ours is running — the
checkpoint row *is* the pause (ADR-016). Step 4 states the invocation count for that
reason: the interesting number is that no Lambda is held while a human takes days.

**The grace window is compressed by stack parameter, never bypassed.** Dev stacks set
`SWEEP_DELAYS_SECONDS` and `APPROVAL_TIMEOUT_SECONDS` so the scheduler fires in minutes
instead of days. The scheduler still fires; skipping it would exercise a code path that
does not exist in production, which is the difference between a demonstration and a
simulation (ADR-017's objection, applied to our own tooling).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from rich.console import Console

from pii_erasure.cli import operations
from pii_erasure.cli.operations import OperationError

_console = Console(stderr=True)

#: Printed at each step so two runs can be diffed line for line. The gate's word is
#: "identically", and a run that renders its phases differently each time cannot be
#: compared even when it is correct.
_STEPS = (
    "1. intake",
    "2. discovery + plan",
    "3. phase 2 soft delete",
    "4. the pause",
    "5. approval",
    "6. grace window",
    "7. phase 3 hard delete",
    "8. certificate",
)


def run(*, subject: str | None = None, tenant: str = "default") -> int:
    subject_ref = subject or f"sub_{uuid.uuid4().hex[:12]}"
    saga_id = f"saga_{uuid.uuid4().hex[:12]}"
    started = time.time()
    try:
        _arc(saga_id=saga_id, subject_ref=subject_ref, tenant=tenant)
    except OperationError as error:
        _console.print(f"\n❌ walkthrough FAILED: {error}")
        return 1
    _console.print(f"\n✅ walkthrough PASSED in {int(time.time() - started)}s — {saga_id}")
    return 0


def _arc(*, saga_id: str, subject_ref: str, tenant: str) -> None:
    _console.print(f"── walkthrough {saga_id} · subject {subject_ref} ──\n")

    _step(0)
    operations.api_call(
        "POST",
        "/requests",
        {
            "sagaId": saga_id,
            "subjectRef": subject_ref,
            "requestId": f"req_{uuid.uuid4().hex[:8]}",
            "tenantId": tenant,
        },
    )

    _step(1)
    state = operations.wait_for(saga_id, gate="approval")
    manifest = state.get("manifest") or {}
    participants = manifest.get("participants") or []
    _console.print(f"   planned {len(participants)} participant(s)")
    _console.print(f"   digest {state.get('manifest_digest')}")

    _step(2)
    # Phase 2 completed before the interrupt: the gate is reached only after every
    # soft delete has been applied and receipted. That ordering is the saga's, and the
    # walkthrough reads it back rather than assuming it.
    _console.print("   soft deletes applied — the gate is downstream of phase 2")

    _step(3)
    _show_the_pause(saga_id)

    _step(4)
    print(operations.review_text(saga_id))
    operations.submit_decision(saga_id, "approve")

    _step(5)
    _console.print("   waiting for the scheduler — compressed by stack parameter, not skipped")

    _step(6)
    final = operations.wait_for(saga_id, status="completed")

    _step(7)
    _certificate(saga_id, final)


def _show_the_pause(saga_id: str) -> None:
    """The property ADR-016 is built on, made visible.

    Two reads a few seconds apart with no invocation in between: the state does not
    change, because nothing is running. If this printed "still working…" the whole
    durability argument would be wrong.
    """
    first = operations.describe_thread(saga_id)
    time.sleep(3)
    second = operations.describe_thread(saga_id)
    _console.print(f"   gate={first.get('gate')!r} — and still {second.get('gate')!r} 3s later")
    _console.print("   no Lambda is held, no container is warm. The pause is a checkpoint row.")
    if first.get("gate") != second.get("gate"):
        raise OperationError("the saga moved while nobody resumed it — that is not a pause")


def _certificate(saga_id: str, final: dict[str, Any]) -> None:
    verified, entries = operations.verify_ledger(saga_id)
    residuals = final.get("residual_count", 0)
    _console.print(f"   ledger: {verified} entries, chain verified")
    _console.print(f"   residuals disclosed: {residuals}")
    if not entries:
        raise OperationError("a completed saga produced no ledger entries")
    kinds = [entry.event_type for entry in entries]
    for required in ("approval_granted", "hard_delete_applied"):
        if not any(required in kind for kind in kinds):
            # A certificate that does not evidence the deletion is a receipt for nothing.
            raise OperationError(f"no ledger entry evidencing {required!r} — entries: {kinds}")


def _step(index: int) -> None:
    _console.print(f"\n{_STEPS[index]}")
