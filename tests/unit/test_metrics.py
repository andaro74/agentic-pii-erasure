"""The §10.1 metric registry, the EMF documents it builds, and its agreement with the doc.

Three things must say the same thing about what this platform measures: ARCHITECTURE
§10.1's table, `observability/metrics.py`'s registry, and `infra/stacks/observability.py`'s
alarms. The first two are asserted here; the third is `test_observability_synth.py`, which
reads the synthesised template rather than the stack source.

**Agreement is over name *and dimension set*, not name alone.** That distinction is V13-8:
a CloudWatch metric is identified by both, so three checks that compared only names all
passed while an alarm watched a combination nothing published.

The agreement is the whole point. **An alarm on a metric nothing publishes is worse than
no alarm**: it renders green and can never fire, so it actively reports health. That is the
`INSUFFICIENT_DATA` failure V13-3 nearly shipped — the PII scrubber was rewriting EMF
metric *names* to `[REDACTED]`, which would have made every alarm here unfireable while
the code looked correct.

EMF shape is asserted against the published specification rather than against what the
implementation happens to produce — an implementation compared to itself agrees always.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pii_erasure.observability import metrics as m
from pii_erasure.observability.redact import PIIRejectedError, scrub_mapping

REPO = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO / "docs" / "ARCHITECTURE.md"

#: The units EMF accepts, verbatim from the `Unit` pattern in the specification's JSON
#: schema. Listed rather than imported: the point is to check our units against the
#: *published* set, and a copy of our own constant would agree with itself.
_SPEC_UNITS = frozenset(
    {
        "Seconds", "Microseconds", "Milliseconds",
        "Bytes", "Kilobytes", "Megabytes", "Gigabytes", "Terabytes",
        "Bits", "Kilobits", "Megabits", "Gigabits", "Terabits",
        "Percent", "Count", "None",
        "Bytes/Second", "Kilobytes/Second", "Megabytes/Second",
        "Gigabytes/Second", "Terabytes/Second",
        "Bits/Second", "Kilobits/Second", "Megabits/Second",
        "Gigabits/Second", "Terabits/Second", "Count/Second",
    }
)  # fmt: skip

_DIMS = m.Dimensions(stage="dev", plane="saga")


def _documented_metrics() -> list[str]:
    """The metric names in ARCHITECTURE §10.1's table, in order.

    Sliced between the §10.1 heading and the next one so a backticked metric name
    mentioned elsewhere in the document cannot pad the list and make the comparison pass
    for the wrong reason.
    """
    body = ARCHITECTURE.read_text(encoding="utf-8")
    start = body.index("### 10.1 Metrics that matter")
    end = body.index("## 11. Evaluation", start)
    section = body[start:end]
    return [row.group(1) for row in re.finditer(r"^\|\s*`([a-z0-9_.]+)`", section, re.MULTILINE)]


def test_the_doc_section_was_actually_found() -> None:
    """A slice that matched nothing would make the comparison below vacuous."""
    documented = _documented_metrics()
    assert len(documented) >= 10, f"only parsed {documented} from ARCHITECTURE §10.1"


def test_the_registry_and_the_architecture_table_agree() -> None:
    """Either one drifting from the other is a metric that is documented and not measured,
    or measured and not documented. Both are how an observability claim rots."""
    assert [spec.name for spec in m.METRICS] == _documented_metrics(), (
        "observability/metrics.py and ARCHITECTURE §10.1 disagree about which metrics "
        "exist, or about their order"
    )


def test_every_metric_names_why_it_matters() -> None:
    """A threshold with no reason is a number somebody will 'tune' during an incident."""
    for spec in m.METRICS:
        assert spec.why.strip(), f"{spec.name} has no rationale"


def test_the_metrics_app_code_cannot_emit_say_what_publishes_them_instead() -> None:
    """§10.1 tabulates twelve metrics as if they were alike. Two are not: a Lambda killed
    at its timeout runs no more code, and a read by a non-participant principal is only
    visible in CloudTrail. Writing emitters for those would have produced code that can
    never run — so they are marked, and the mechanism is named."""
    unemittable = {spec.name for spec in m.METRICS if not spec.emitted_by_app}
    assert unemittable == set(m.MECHANISM_NOTES), (
        f"{unemittable ^ set(m.MECHANISM_NOTES)} is marked un-emittable without a note, or "
        f"noted without being marked"
    )
    for name, note in m.MECHANISM_NOTES.items():
        assert len(note) > 80, f"{name}'s note does not explain the alternative mechanism"


# ─── the EMF document, against the published specification ────────────────────────────


@pytest.mark.parametrize("spec", m.METRICS, ids=lambda s: s.name)
def test_every_metric_builds_a_specification_shaped_document(spec: m.MetricSpec) -> None:
    document = m.emf_document(spec.name, 1, _DIMS)

    metadata = document["_aws"]
    assert set(metadata) == {"Timestamp", "CloudWatchMetrics"}
    assert isinstance(metadata["Timestamp"], int), "Timestamp MUST be a number, in ms"

    directive = metadata["CloudWatchMetrics"][0]
    assert set(directive) == {"Namespace", "Dimensions", "Metrics"}
    assert directive["Namespace"] == m.NAMESPACE

    # A DimensionSet array: an array OF arrays. Flattening it is the classic mistake and
    # produces a document CloudWatch drops.
    assert isinstance(directive["Dimensions"], list)
    assert all(isinstance(dimension_set, list) for dimension_set in directive["Dimensions"])
    assert len(directive["Dimensions"][0]) <= 30, "a DimensionSet MUST NOT exceed 30 keys"

    definition = directive["Metrics"][0]
    assert definition["Name"] == spec.name
    assert definition["Unit"] in _SPEC_UNITS, f"{definition['Unit']} is not a CloudWatch unit"

    # Targets MUST be members of the ROOT node, never nested, and metric targets MUST be
    # numeric while dimension targets MUST be strings. Checked for EVERY set, not just the
    # first — a set whose members are not on the root node is dropped by CloudWatch, and
    # dropping the wide set silently would look identical to not publishing it.
    for dimension_set in directive["Dimensions"]:
        for key in dimension_set:
            assert key in document, f"dimension {key} is not a root member"
            assert isinstance(document[key], str)
    assert isinstance(document[spec.name], (int, float))
    assert not isinstance(document[spec.name], bool)


# ─── the dimension SET, which is part of the metric's identity ────────────────────────


@pytest.mark.parametrize(
    "dimensions",
    [
        m.Dimensions(stage="dev", plane="saga"),
        m.Dimensions(stage="dev", plane="saga", system_id="billing-ledger"),
        m.Dimensions(stage="dev", plane="policy", system_id="subject.hard_delete"),
    ],
    ids=["base", "per-participant", "per-action"],
)
def test_the_base_dimension_set_is_published_whatever_the_call_site_passes(
    dimensions: m.Dimensions,
) -> None:
    """V13-8, and the fourth way to build an alarm that cannot fire.

    `{stage, plane}` and `{stage, plane, systemId}` are two different metrics in
    CloudWatch that share a name; EMF rolls up across neither. `hard_delete` dimensioned
    `phase3.stuck_participants` by participant, so the §10.1 alarm — written against
    `{stage, plane}`, as every alarm in the stack is — watched a combination nothing ever
    published. With `TreatMissingData: NOT_BREACHING` it rendered green forever, on the
    one metric whose whole purpose is to say a statutory deadline is forming.

    So the base set is emitted unconditionally. This is the property the alarms rest on.
    """
    sets = m.emf_document("phase3.stuck_participants", 1, dimensions)["_aws"]["CloudWatchMetrics"][
        0
    ]["Dimensions"]
    assert ["plane", "stage"] in sets, (
        f"{sets} omits the base set, so every alarm on this metric watches nothing"
    )


def test_an_optional_dimension_adds_a_set_rather_than_replacing_one() -> None:
    """The per-participant breakdown must survive too — "which system is stuck" is the
    operator's first question, and answering it is why the dimension was added at all."""
    dimensions = m.Dimensions(stage="dev", plane="saga", system_id="billing-ledger")
    sets = m.emf_document("phase3.stuck_participants", 1, dimensions)["_aws"]["CloudWatchMetrics"][
        0
    ]["Dimensions"]
    assert sets == [["plane", "stage"], ["plane", "stage", "systemId"]]


def test_the_base_set_is_not_duplicated_when_there_is_nothing_to_widen() -> None:
    """A repeated set would bill twice for one metric and is what a naive `[base, wide]`
    produces when they are equal."""
    sets = m.Dimensions(stage="dev", plane="saga").dimension_sets()
    assert sets == [["plane", "stage"]]


def test_an_unregistered_metric_is_refused() -> None:
    """A typo publishes a metric no alarm watches, which reads exactly like health."""
    with pytest.raises(m.MetricError, match=r"10\.1"):
        m.emf_document("saga.durtaion", 1, _DIMS)


def test_a_pii_shaped_dimension_is_refused_rather_than_scrubbed() -> None:
    """EMF bills one custom metric per unique dimension combination, so a subject-derived
    dimension is a cost defect and an invariant 5 defect at once. Scrubbing it would split
    the metric across a redacted value and hide both."""
    with pytest.raises(PIIRejectedError):
        m.emf_document("policy.deny", 1, m.Dimensions(stage="ada@example.invalid", plane="saga"))


def test_dimensions_cannot_carry_a_subject_reference() -> None:
    """Enforced by the shape, not by review: there is no field to put one in."""
    assert not any("subject" in field.lower() for field in m.Dimensions.__dataclass_fields__)


def test_a_metric_document_survives_the_logging_scrubber_intact() -> None:
    """The end-to-end version of V13-3, since the emitter and the scrubber are what
    actually meet in production. Metric names, namespace and units must come through
    byte-identical, or CloudWatch extracts a metric no alarm is watching."""
    document = m.emf_document("manifest.digest_mismatch", 1, _DIMS)
    scrubbed = scrub_mapping(document)
    assert scrubbed["_aws"] == document["_aws"]
    assert scrubbed["manifest.digest_mismatch"] == 1


# ─── the registry must match the call sites ───────────────────────────────────────────


def _module_source(dotted: str) -> str:
    """`saga.nodes.verify` → the file, whether under `pii_erasure/` or the repo root."""
    relative = Path(*dotted.split(".")).with_suffix(".py")
    for base in (REPO / "src" / "pii_erasure", REPO):
        candidate = base / relative
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError(f"{dotted} names no module — {relative} is not under src/ or the root")


@pytest.mark.parametrize("spec", [s for s in m.METRICS if s.emitter], ids=lambda s: s.name)
def test_a_named_emitter_really_emits_that_metric(spec: m.MetricSpec) -> None:
    """The registry claims a module publishes this metric. Check the call is there.

    Without this the registry is a wish: it would keep asserting agreement with
    ARCHITECTURE §10.1 and with the alarms while nothing wrote a data point, and every
    alarm would read as healthy for the most boring possible reason.
    """
    assert spec.emitter is not None  # narrowed for mypy; the parametrize filters these
    source = _module_source(spec.emitter)
    assert f'"{spec.name}"' in source, (
        f"{spec.emitter} is recorded as the emitter of {spec.name} but never names it"
    )
    # `emit(` or `emit_elapsed(`: the two duration metrics go through a helper that owns
    # the "start moment unknown → publish nothing" rule, so it exists in one place rather
    # than at every call site. The name still has to appear here, which is the half of
    # this check that catches a metric wired to the wrong emitter.
    assert re.search(r"\bemit\w*\(", source), f"{spec.emitter} calls no emit function"


def test_every_metric_is_either_emitted_noted_or_pending() -> None:
    """No fourth state. A metric with no emitter, no mechanism note and no pending reason
    is one somebody meant to wire and nothing will remind them about."""
    for spec in m.METRICS:
        if spec.emitter:
            continue
        explained = spec.name in m.MECHANISM_NOTES or spec.name in m.PENDING_EMITTERS
        assert explained, (
            f"{spec.name} has no emitter, and neither MECHANISM_NOTES (cannot be emitted "
            f"by app code) nor PENDING_EMITTERS (not wired yet, and why) accounts for it"
        )


def test_the_two_dicts_do_not_overlap() -> None:
    """ "Cannot be emitted by application code" and "not wired yet" are different claims.
    A metric in both would let a permanent limitation masquerade as a to-do, or the
    reverse — and the reverse is how a to-do stops being one."""
    overlap = set(m.MECHANISM_NOTES) & set(m.PENDING_EMITTERS)
    assert not overlap, f"{overlap} is described as both un-emittable and merely pending"


def test_pending_metrics_are_marked_as_app_emittable() -> None:
    """The distinction has to hold in the data too, not just in the prose: a pending
    metric is one app code *will* emit, so `emitted_by_app` must already say so."""
    for name in m.PENDING_EMITTERS:
        assert m.BY_NAME[name].emitted_by_app, (
            f"{name} is pending an emitter but marked as not app-emittable — one of the two "
            f"is wrong"
        )
