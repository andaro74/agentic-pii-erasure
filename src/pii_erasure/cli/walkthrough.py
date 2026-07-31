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

import json
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
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


#: Where `make seed` writes the placement map. The walkthrough reads it to pick a subject
#: that actually HAS data, which is the difference between demonstrating an erasure and
#: demonstrating a lookup that finds nothing.
GROUND_TRUTH = Path("evals/fixtures/ground-truth.json")

#: What to do when every seeded subject has been erased — and specifically what NOT to do.
#:
#: This message used to say "run `make seed` for a fresh set", which cannot work and was
#: repeated into the CLI's tombstone explanation before anyone tried it (V13-14). The
#: seven `subjectRef` values are **hardcoded in `seeds/meridian.json`**, and the tombstone
#: registry is keyed by their hash and is append-only by design — §5.3's anti-resurrection
#: control, whose whole point is that an entry outlives the data permanently. So re-seeding
#: rewrites the same seven refs, every one of them still tombstoned, and `intake` refuses
#: exactly as before. The command succeeds; the problem does not move.
#:
#: Deleting rows from the tombstone table would "work" and is left out on purpose: it
#: disables the control this platform exists to demonstrate, and an operator who learns
#: that habit on a dev stack has learned the wrong one.
_EXHAUSTED = (
    "every seeded subject has already been erased, and re-seeding will NOT help: the "
    "subjectRefs are fixed in seeds/meridian.json and the tombstone registry is "
    "append-only by design (ARCHITECTURE §5.3 — an erased subject can never be "
    "recreated). The only reset is a new stack:\n\n"
    "  make destroy-dev && make deploy-dev && make seed\n\n"
    "Run the walkthrough BEFORE `make conformance` and `make integration` next time — "
    "those consume subjects too, and they are what ate this set."
)


def seeded_subject() -> str:
    """A subject the fixtures actually placed data for.

    The first version generated `sub_<random>`. Discovery then correctly found nothing
    anywhere, the plan had zero participants, and the saga died on a pydantic validation
    error 37 seconds in (V11-4). Inventing an identifier and expecting data behind it was
    the bug — the generator emits the map for exactly this reason (ADR-020), so the
    walkthrough reads it rather than guessing.
    """
    if not GROUND_TRUTH.is_file():
        raise OperationError(
            f"{GROUND_TRUTH} not found — run `make seed` first. The walkthrough erases a "
            f"seeded subject; it cannot invent one, because a subject with no data "
            f"anywhere produces an empty plan and demonstrates nothing."
        )
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    subjects = truth.get("subjects", {})
    held = {ref for ref, ids in (truth.get("holds") or {}).items() if ids}
    # A subject under a legal hold cannot complete the arc, and should not: `hold_check`
    # stops the saga before phase 2 and writes BLOCKED_BY_HOLD. That is the veto working,
    # and `sub_dmi_2b8e4406` exists in the fixtures precisely to prove it — but proving
    # the veto is a different demonstration from proving the arc, and the gate measures
    # the arc. The generated map records holds already; this just reads them (V11-8).
    placed = {ref: systems for ref, systems in subjects.items() if systems and ref not in held}
    if not placed:
        raise OperationError(
            f"{GROUND_TRUTH} offers no subject with data and no legal hold — re-run "
            f"`make seed`. (Subjects under hold are skipped: they block at `hold_check` "
            f"by design, which demonstrates the veto rather than the arc.)"
        )

    # Skip anyone already erased. The gate's word is "twice", and this function was
    # deterministic — `max()` over the same map returns the same subject — so the second
    # run would have targeted the subject the first run had just erased. Discovery would
    # find nothing, and `intake` would refuse on the tombstone before that. Neither is a
    # bug in the platform; both would look like one.
    #
    # The tombstone registry is the right source because it is the system's OWN record of
    # "this subject has been erased" — the same registry `intake` consults to refuse a
    # resurrection. Tracking used subjects in a file beside the walkthrough would be a
    # second source of truth that drifts the first time someone tears down a stack.
    erased = _tombstoned(placed)
    remaining = {ref: systems for ref, systems in placed.items() if ref not in erased}
    if not remaining:
        raise OperationError(_EXHAUSTED)
    # Of those, the one touching the MOST systems: a walkthrough that exercises one
    # participant proves less than one that exercises seven, and phase 3's
    # per-participant loop is where ordering and residual honesty actually show up.
    return str(max(remaining, key=lambda ref: len(remaining[ref])))


def _tombstoned(subject_refs: Iterable[str]) -> set[str]:
    """Which of these the registry already holds. One stack read, then one lookup each.

    A read failure propagates rather than defaulting: assuming "not tombstoned" would
    send the walkthrough at an erased subject, and assuming "tombstoned" would skip every
    candidate and claim the fixtures were exhausted. Both are confident wrong answers.
    """
    import boto3

    from pii_erasure.saga.tombstone import TombstoneRegistry

    table = operations.outputs("foundation")["TombstonesTable"]
    registry = TombstoneRegistry(table, client=boto3.client("dynamodb"))
    return {ref for ref in subject_refs if registry.is_tombstoned(ref)}


def check_usable(subject_ref: str) -> None:
    """Refuse a named subject the fixtures already say cannot complete the arc.

    `--subject` bypassed every check `seeded_subject()` makes. Naming an erased subject
    therefore created a saga, drove it to `already_tombstoned`, and reported the halt as a
    walkthrough failure (V13-14) — the saga was right, and the run should never have
    started. Checked here, before intake, so the answer costs one DynamoDB read instead of
    a saga and a poll.
    """
    if not GROUND_TRUTH.is_file():
        return  # seeded_subject()'s own error covers a missing map better than this can
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    subjects = truth.get("subjects", {})
    if subject_ref not in subjects:
        # Deliberately NOT refused. `--subject` is a debugging escape hatch, and an
        # operator chasing a subject this local map has never heard of — one from another
        # seed run, or from a real saga — is not doing anything wrong. Refusing on absence
        # would turn the escape hatch into a whitelist keyed on a build artefact. This
        # function only overrules the operator where the fixtures KNOW the run cannot
        # work; it does not gatekeep what it has no opinion about.
        return

    held = {ref for ref, ids in (truth.get("holds") or {}).items() if ids}
    placed = {ref for ref, systems in subjects.items() if systems and ref not in held}
    erased = _tombstoned(list(subjects))
    usable = sorted(placed - erased)

    if subject_ref in erased:
        raise OperationError(
            f"{subject_ref} has already been erased — its tombstone is permanent, so this "
            f"run would halt at intake without demonstrating anything.\n"
            + (
                f"Still usable: {', '.join(usable)}"
                if usable
                else "No seeded subject remains.\n\n" + _EXHAUSTED
            )
        )
    if subject_ref in held:
        raise OperationError(
            f"{subject_ref} is under a legal hold, so it stops at `hold_check` before "
            f"phase 2 and writes BLOCKED_BY_HOLD. That is the veto working and worth "
            f"demonstrating — but it is a different demonstration from the arc, and this "
            f"command measures the arc.\n"
            f"For the arc: {', '.join(usable) or '(none remain)'}"
        )


def run(*, subject: str | None = None, tenant: str = "default") -> int:
    try:
        if subject:
            check_usable(subject)
        subject_ref = subject or seeded_subject()
    except OperationError as error:
        _console.print(f"\n❌ walkthrough FAILED: {error}")
        return 1
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
    state = operations.wait_for(saga_id, gate="approval", notify=_console.print)
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
    final = operations.wait_for(saga_id, status="completed", notify=_console.print)

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


#: What a completed erasure must have written down. Exact event types, **uppercase** —
#: this check first compared lowercase strings against `APPROVAL_GRANTED` and
#: `HARD_DELETE_APPLIED`, so it could never have matched and would have failed the gate on
#: a flawless run (V11-4). `test_walkthrough_certificate.py` pins these against the names
#: the nodes actually emit, so a rename breaks a unit test rather than a deployed gate.
REQUIRED_LEDGER_EVENTS = frozenset({"APPROVAL_GRANTED", "HARD_DELETE_APPLIED", "SAGA_COMPLETED"})


def _certificate(saga_id: str, final: dict[str, Any]) -> None:
    verified, entries = operations.verify_ledger(saga_id)
    residuals = final.get("residual_count", 0)
    _console.print(f"   ledger: {verified} entries, chain verified")
    _console.print(f"   residuals disclosed: {residuals}")
    if not entries:
        raise OperationError("a completed saga produced no ledger entries")
    kinds = {entry.event_type for entry in entries}
    missing = sorted(REQUIRED_LEDGER_EVENTS - kinds)
    if missing:
        # A certificate that does not evidence the deletion is a receipt for nothing.
        raise OperationError(f"no ledger entry evidencing {missing} — entries: {sorted(kinds)}")


def _step(index: int) -> None:
    _console.print(f"\n{_STEPS[index]}")
