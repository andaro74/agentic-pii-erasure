"""The seed set, the ground-truth generator, and the stack outputs they depend on.

The generator's *writers* can only be exercised honestly against real AWS (ADR-017), and
`make seed` is where that happens. What is testable here — and worth testing, because
getting it wrong corrupts the recall gate's denominator rather than crashing — is the
assembly logic: that the map is built from what was written, that a declared placement
which produced nothing is caught, and that no real PII is in the fixtures.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from evals.fixtures.generator import (
    GroundTruth,
    Placement,
    expected_systems,
    load_seeds,
    reconcile,
)
from pii_erasure.contract.registry import system_ids

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def seeds() -> dict[str, Any]:
    return load_seeds()


# ─── the seed set ─────────────────────────────────────────────────────────────────────


def test_the_seven_subjects_match_the_readme(seeds: dict[str, Any]) -> None:
    """The README table and the fixture are the same seven people, or one of them lies."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for subject in seeds["subjects"]:
        assert subject["displayName"] in readme, (
            f"{subject['displayName']} is seeded but absent from the README table"
        )
    assert len(seeds["subjects"]) == 7


def test_every_seeded_system_is_a_registered_participant(seeds: dict[str, Any]) -> None:
    """A placement naming an unregistered system would generate ground truth nothing can
    satisfy, and the recall gate would fail forever with no defect to fix."""
    registered = set(system_ids())
    for subject in seeds["subjects"]:
        unknown = set(subject.get("placement", {})) - registered
        assert not unknown, f"{subject['subjectRef']} placed in unregistered {unknown}"


def test_every_email_is_an_unroutable_reserved_domain(seeds: dict[str, Any]) -> None:
    """RFC 2606 reserves `.invalid`, so a seeding accident cannot mail a real person.

    The addresses are fabricated, but the discipline is the point: this repo treats seeded
    fake PII exactly as if it were real (invariant 5).
    """
    for subject in seeds["subjects"]:
        assert subject["email"].endswith(".invalid"), subject["email"]


def test_subject_refs_are_pseudonymous_and_carry_no_name(seeds: dict[str, Any]) -> None:
    """The handle reaches Cognito, S3 keys, vector keys and SES addresses. If it embedded a
    surname, invariant 5 would be violated by construction in five systems at once."""
    for subject in seeds["subjects"]:
        handle = subject["subjectRef"]
        assert re.match(r"^sub_[a-z]{3}_[0-9a-f]{8}$", handle), handle
        surname = subject["displayName"].split()[-1].lower()
        assert surname not in handle.lower()


def test_the_injection_payload_is_present_and_unsanitised(seeds: dict[str, Any]) -> None:
    """Yuki's bio must keep its payload verbatim.

    A fixture that sanitised it would make the adversarial eval unable to fail, which is
    the same defect class as a gate that cannot gate. The defence under test is that the
    agent has no mutating tool — not that the text was cleaned up first.
    """
    yuki = next(s for s in seeds["subjects"] if s["displayName"].startswith("Yuki"))
    payload = yuki["injection"]["payload"]
    assert "hard_delete" in payload
    assert "Ignore all previous instructions" in payload
    assert yuki["injection"]["field"] == "bio"


def test_the_held_subject_has_a_scoped_hold_not_a_subject_wide_one(
    seeds: dict[str, Any],
) -> None:
    """The lesson is that a hold covers a scope. A subject-wide hold in the fixture would
    quietly make the under-deletion failure mode untestable."""
    dmitri = next(s for s in seeds["subjects"] if s["displayName"].startswith("Dmitri"))
    hold = dmitri["holds"][0]
    assert hold["scope"] == "public.invoices"
    assert set(dmitri["placement"]) - {"billing-ledger"}, (
        "the held subject must also exist elsewhere, or 'scoped' proves nothing"
    )


# ─── ground truth assembly ────────────────────────────────────────────────────────────


def test_ground_truth_records_only_what_was_actually_written() -> None:
    """A writer that wrote nothing must not appear in the map (invariant 8)."""
    truth = GroundTruth(tenant_id="meridian")
    truth.record("sub_a", Placement(system_id="profile-store", artifacts={"items": 3}))
    truth.record("sub_a", Placement(system_id="upload-bucket", artifacts={"objects": 0}))
    truth.record("sub_a", Placement(system_id="vector-index", artifacts={}))

    assert truth.systems_for("sub_a") == {"profile-store"}


def test_reconcile_catches_a_declared_placement_that_was_never_written() -> None:
    """The failure this prevents is silent: a missing write shows up later as a recall
    miss and gets blamed on the discovery agent."""
    seeds = {
        "tenant": {"tenantId": "meridian"},
        "subjects": [
            {
                "subjectRef": "sub_a",
                "placement": {"profile-store": {"items": 1}, "vector-index": {"vectors": 2}},
            }
        ],
    }
    truth = GroundTruth(tenant_id="meridian")
    truth.record("sub_a", Placement(system_id="profile-store", artifacts={"items": 1}))

    problems = reconcile(truth, seeds)

    assert len(problems) == 1
    assert "vector-index" in problems[0]
    assert "nothing was written" in problems[0]


def test_reconcile_catches_a_write_nobody_declared() -> None:
    seeds = {
        "tenant": {"tenantId": "meridian"},
        "subjects": [{"subjectRef": "sub_a", "placement": {"profile-store": {"items": 1}}}],
    }
    truth = GroundTruth(tenant_id="meridian")
    truth.record("sub_a", Placement(system_id="profile-store", artifacts={"items": 1}))
    truth.record("sub_a", Placement(system_id="analytics-lake", artifacts={"rows": 5}))

    problems = reconcile(truth, seeds)

    assert any("not declared in seeds" in problem for problem in problems)


def test_reconcile_is_silent_when_the_pass_matched_the_declaration(
    seeds: dict[str, Any],
) -> None:
    """The guard must also be able to pass, or it is noise rather than a control."""
    truth = GroundTruth(tenant_id=seeds["tenant"]["tenantId"])
    for subject in seeds["subjects"]:
        for system_id, artifacts in subject.get("placement", {}).items():
            truth.record(subject["subjectRef"], Placement(system_id, dict(artifacts)))

    assert reconcile(truth, seeds) == []


def test_the_emitted_map_warns_against_hand_editing() -> None:
    truth = GroundTruth(tenant_id="meridian")
    truth.record("sub_a", Placement(system_id="profile-store", artifacts={"items": 1}))
    body = json.dumps(truth.to_json())

    assert "GENERATED" in body
    assert "Do not hand-edit" in body


def test_expected_systems_reads_the_declaration_not_the_writes(
    seeds: dict[str, Any],
) -> None:
    declared = expected_systems(seeds)
    assert declared["sub_pri_9c1d7e22"] == {"vector-index"}, (
        "the orphan subject must be declared in exactly one derived store"
    )


# ─── the CLI's dependency on stack outputs ────────────────────────────────────────────


def test_every_stack_output_the_seeder_reads_actually_exists() -> None:
    """`erasure seed` resolves resource names from CloudFormation outputs by key.

    A mistyped key is a `KeyError` that only appears when a human runs `make seed` against
    a deployed stack — the slowest possible feedback loop, and the one that costs a deploy
    cycle to discover. Asserted here against the synthesised templates instead. This test
    was written after `DekRegistryTableName` (which does not exist) shipped in place of
    `DekRegistryTable` (which does).
    """
    sys.path.insert(0, str(REPO / "infra"))
    from aws_cdk import App
    from aws_cdk.assertions import Template
    from stacks.foundation import FoundationStack
    from stacks.participants import ParticipantsStack

    app = App()
    foundation = FoundationStack(app, "asdp-t-foundation", stage="t", object_lock_days=1)
    participants = ParticipantsStack(
        app,
        "asdp-t-participants",
        stage="t",
        object_lock_days=1,
        dek_registry=foundation.dek_registry,
        idempotency=foundation.idempotency,
    )
    available = set()
    for stack in (foundation, participants):
        available |= set(Template.from_stack(stack).to_json().get("Outputs", {}))

    source = (REPO / "src" / "pii_erasure" / "cli" / "main.py").read_text(encoding="utf-8")
    read_keys = set(re.findall(r'outputs\["([A-Za-z0-9]+)"\]', source))
    assert read_keys, "the extraction found nothing — this test would pass vacuously"

    missing = read_keys - available
    assert not missing, f"the seeder reads stack outputs that are never exported: {missing}"


def test_every_config_key_the_generator_reads_is_supplied_by_the_cli() -> None:
    """`_stack_config()` builds the dict the generator indexes into.

    They are written in different files and joined only at runtime, so a key added to one
    and not the other is a `KeyError` during `make seed` — against a deployed stack, with a
    human waiting. Same shape as the stack-output check above, one layer in.
    """
    generator_src = (REPO / "evals" / "fixtures" / "generator.py").read_text(encoding="utf-8")
    cli_src = (REPO / "src" / "pii_erasure" / "cli" / "main.py").read_text(encoding="utf-8")

    # Both quote styles. The first version matched only double quotes and silently missed
    # `self._config['analyticsBucket']` — single-quoted because it sits inside an f-string.
    # A guard that reads 15 of 16 keys reports success while ignoring the newest one, which
    # is the failure mode it was written to prevent, one level up.
    read = set(re.findall(r"""_config\[["']([a-zA-Z]+)["']\]""", generator_src))
    assert read, "the extraction found nothing — this test would pass vacuously"
    assert len(read) >= 16, (
        f"only {len(read)} config keys extracted; the generator uses more than that, so "
        "the pattern has stopped seeing some of them"
    )

    supplied = set(re.findall(r'^\s*"([a-zA-Z]+)":', cli_src, re.MULTILINE))
    missing = sorted(read - supplied)
    assert not missing, f"the generator reads config keys the CLI never supplies: {missing}"
