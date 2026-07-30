"""The observability stack, asserted against the synthesised template.

This closes the three-way agreement M10 needs: ARCHITECTURE §10.1's table, the emitters in
`observability/metrics.py`, and the alarms in the deployed stack must describe the same set
of metrics. The first two are checked in `test_metrics.py`; this file checks the third, and
it reads the **template** rather than the stack source — the template is what CloudFormation
acts on, and a stack that constructs an alarm and never adds it to itself would pass a
source-level check.

The property that matters most is negative: **no alarm may exist for a metric nothing
publishes.** Such an alarm never leaves `INSUFFICIENT_DATA`, so it renders as untriggered
and reports health it cannot observe. Every metric without an alarm must appear in
`NOT_ALARMED` with a reason, which is what keeps "we forgot one" from reading like "we
decided not to".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from pii_erasure.observability.metrics import BY_NAME, METRICS, NAMESPACE

# Same idiom as the other synth tests: `infra/` is a CDK app directory, not a package on
# the install path, so it is added here rather than imported by anything under `src/`.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "infra"))

from stacks.observability import (  # noqa: E402 — needs the path insert above
    NOT_ALARMED,
    STACK_PUBLISHED,
    ObservabilityStack,
    _plane_of,
    alarmed_metrics,
)

STAGE = "test"

#: How each emitter names the `plane` dimension at its call site. `plane="x"` covers the
#: modules that build `Dimensions` directly; `metric_dimensions("x")` covers the saga
#: nodes, which go through `SagaDeps` so every node agrees.
_PLANE_AT_CALL_SITE = re.compile(r'plane="(\w+)"|metric_dimensions\(\s*"(\w+)"')


@pytest.fixture(scope="module")
def template() -> Any:
    from aws_cdk import App, assertions

    app = App(context={"stage": STAGE})
    stack = ObservabilityStack(app, f"asdp-{STAGE}-observability", stage=STAGE)
    return assertions.Template.from_stack(stack)


def _alarms(template: Any) -> dict[str, dict[str, Any]]:
    return {
        body["Properties"]["MetricName"]: body["Properties"]
        for body in template.find_resources("AWS::CloudWatch::Alarm").values()
    }


def test_the_template_has_alarms_at_all(template: Any) -> None:
    """A find_resources that returned nothing would make every assertion below vacuous."""
    assert _alarms(template), "the stack synthesised no alarms"


def test_every_alarm_watches_a_metric_something_publishes(template: Any) -> None:
    """The negative property, and the reason this file exists.

    An alarm on an unpublished metric sits in INSUFFICIENT_DATA and shows as untriggered —
    indistinguishable, on a dashboard, from a system that is fine.
    """
    for name in _alarms(template):
        spec = BY_NAME.get(name)
        assert spec is not None, f"alarm on {name}, which is not in ARCHITECTURE §10.1"
        published = bool(spec.emitter) or name in STACK_PUBLISHED
        assert published, (
            f"{name} has an alarm and no emitter — it would never leave INSUFFICIENT_DATA "
            f"and would report health it cannot see"
        )


def test_every_publishable_metric_with_a_threshold_is_alarmed(template: Any) -> None:
    """The positive direction. A metric that is emitted, has a threshold, and has no alarm
    is a control described in §10.1 and absent from the account."""
    alarmed = set(_alarms(template))
    assert alarmed == {spec.name for spec in alarmed_metrics()}, (
        f"the template's alarms and alarmed_metrics() disagree: "
        f"{alarmed ^ {s.name for s in alarmed_metrics()}}"
    )


def test_every_metric_without_an_alarm_has_a_recorded_reason(template: Any) -> None:
    """No silent omissions. This is the difference between a decision and an oversight, and
    the two are indistinguishable six months later."""
    unalarmed = {spec.name for spec in METRICS} - set(_alarms(template))
    assert unalarmed == set(NOT_ALARMED), (
        f"{unalarmed ^ set(NOT_ALARMED)} is unalarmed without a reason in NOT_ALARMED, or "
        f"has a reason recorded while being alarmed anyway"
    )
    for name, reason in NOT_ALARMED.items():
        assert len(reason) > 60, f"{name}'s reason does not explain the decision"


@pytest.mark.parametrize("spec", alarmed_metrics(), ids=lambda s: s.name)
def test_alarms_treat_missing_data_as_not_breaching(spec: Any, template: Any) -> None:
    """EMF publishes no data point when nothing happens, and for most of §10.1 "nothing
    happened" IS the healthy state. Left at the CloudWatch default these alarms would never
    leave INSUFFICIENT_DATA — the same defect as alarming on an unpublished metric, reached
    from the other side."""
    properties = _alarms(template)[spec.name]
    assert properties["TreatMissingData"] == "notBreaching", (
        f"{spec.name} would sit in INSUFFICIENT_DATA between events"
    )


@pytest.mark.parametrize("spec", alarmed_metrics(), ids=lambda s: s.name)
def test_every_alarm_watches_a_dimension_set_the_emitter_publishes(
    spec: Any, template: Any
) -> None:
    """The fourth way to build an alarm that cannot fire, and the one V13-8 shipped.

    A dimension set is part of a metric's identity: `{stage, plane}` and
    `{stage, plane, systemId}` are two metrics sharing a name, and EMF rolls up across
    neither. `hard_delete` dimensioned `phase3.stuck_participants` by participant while
    this stack alarmed on `{stage, plane}` — so the alarm watched a combination nothing
    published, and `TreatMissingData: NOT_BREACHING` rendered it green rather than
    `INSUFFICIENT_DATA`, which is the *only* state the other three checks in this file
    look for.

    The equality is asserted against `Dimensions.dimension_sets()` rather than against a
    literal, so the emitter is what defines the answer. Widening the alarm without
    widening the emitter fails here.
    """
    from pii_erasure.observability.metrics import Dimensions

    alarmed = {d["Name"] for d in _alarms(template)[spec.name]["Dimensions"]}
    # What the emitter would publish at its *widest* call site: system_id set. The base
    # set has to be in there, and the alarm has to be the base set.
    published = Dimensions(stage=STAGE, plane="saga", system_id="any").dimension_sets()
    assert sorted(alarmed) in published, (
        f"{spec.name}'s alarm watches {sorted(alarmed)}, which is not one of the "
        f"dimension sets the emitter publishes ({published}) — it can never receive a "
        f"data point, and NOT_BREACHING makes that look healthy rather than unknown"
    )


def test_the_stack_alarms_on_the_base_dimension_set_and_nothing_wider(template: Any) -> None:
    """The other half: the alarm set must be the one that is published *unconditionally*.

    A per-participant alarm would be correct only for the call sites that pass a
    `systemId`, and `hard_delete` emits `manifest.digest_mismatch` without one. One shape
    for every alarm is what makes `_alarm` generic rather than per-metric."""
    for name, properties in _alarms(template).items():
        assert sorted(d["Name"] for d in properties["Dimensions"]) == ["plane", "stage"], (
            f"{name} is alarmed on a wider set than every call site guarantees"
        )


@pytest.mark.parametrize("spec", [s for s in METRICS if s.emitter], ids=lambda s: s.name)
def test_the_plane_the_stack_alarms_on_is_the_plane_the_emitter_writes(spec: Any) -> None:
    """`_plane_of` says it derives the plane "so the two cannot drift silently" — and
    nothing was checking, which is a no-drift claim with no mechanism behind it.

    The dimension's VALUE is as much a part of the metric's identity as its key, so
    alarming on `plane=saga` while `policy/engine.py` emits `plane=policy` is the same
    dead alarm as V13-8 reached from one field over.
    """
    assert spec.emitter is not None
    relative = Path(*spec.emitter.split(".")).with_suffix(".py")
    for base in (REPO / "src" / "pii_erasure", REPO):
        if (base / relative).is_file():
            source = (base / relative).read_text(encoding="utf-8")
            break
    else:  # pragma: no cover — test_metrics.py fails first on a bad emitter path
        raise AssertionError(f"{spec.emitter} names no module")

    # The two duration metrics go through `_shared.emit_elapsed`, which owns the "start
    # moment unknown → publish nothing" rule; the plane is set there rather than at the
    # node. Follow the helper rather than exempting its callers, or the metric with the
    # longest fuse — time to approval decision — would be the one nothing checked.
    if "emit_elapsed" in source:
        source += (REPO / "src" / "pii_erasure" / "saga" / "nodes" / "_shared.py").read_text(
            encoding="utf-8"
        )

    planes = {a or b for a, b in _PLANE_AT_CALL_SITE.findall(source)}
    assert planes, f"{spec.emitter} sets no plane dimension this test can read"
    assert _plane_of(spec) in planes, (
        f"the stack alarms {spec.name} on plane={_plane_of(spec)!r} but {spec.emitter} "
        f"emits plane={planes} — the alarm watches a metric nobody writes"
    )


def test_the_recall_alarm_fires_on_low_not_high(template: Any) -> None:
    """`discovery.recall` is the one metric where low is the failure. An alarm built with
    the comparison reversed is permanently green on the number ADR-008 calls P1 — and it
    would look exactly like a healthy gate."""
    properties = _alarms(template)["discovery.recall"]
    assert properties["ComparisonOperator"] == "LessThanThreshold"
    assert properties["Threshold"] == 1.0


def test_every_alarm_notifies_somewhere(template: Any) -> None:
    """An alarm with no action is a red square nobody is looking at."""
    for name, properties in _alarms(template).items():
        assert properties.get("AlarmActions"), f"{name} fires into the void"


def test_alarms_and_the_dashboard_use_the_registry_namespace(template: Any) -> None:
    """A namespace typo produces an alarm on a metric that will never exist, which is the
    unpublished-metric failure wearing a different hat."""
    for name, properties in _alarms(template).items():
        assert properties["Namespace"] == NAMESPACE, f"{name} watches the wrong namespace"


def test_the_executor_timeout_metric_is_extracted_from_logs(template: Any) -> None:
    """The metric no application code can emit: a Lambda killed at its timeout runs no
    further code. If this filter is absent, its alarm is watching nothing."""
    template.resource_count_is("AWS::Logs::MetricFilter", 1)
    body = next(iter(template.find_resources("AWS::Logs::MetricFilter").values()))
    transformation = body["Properties"]["MetricTransformations"][0]
    assert transformation["MetricName"] == "saga.executor_timeout"
    assert transformation["MetricNamespace"] == NAMESPACE
    assert "Task timed out" in body["Properties"]["FilterPattern"]

    # The filter must publish the SAME dimension set the alarm watches, for the reason
    # V13-8 established: a dimension set is part of a metric's identity, and this is the
    # one §10.1 metric whose publisher is the stack rather than application code — so
    # nothing in `metrics.py` constrains it and it has to be checked here.
    published = {pair["Key"]: pair["Value"] for pair in transformation["Dimensions"]}
    assert published == {"stage": STAGE, "plane": "saga"}

    # And the pair AWS refuses, asserted here too rather than only in the sweep below —
    # this is the filter that actually failed a deploy.
    assert "DefaultValue" not in transformation


def test_no_metric_filter_sets_both_dimensions_and_a_default_value() -> None:
    """AWS rejects the combination at deploy time; `cdk synth` never sees it (V13-11).

    *"If you assign dimensions to a metric created by a metric filter, you can't assign a
    default value for that metric."* CloudFormation returns a 400 — after the change set
    is accepted, which is the expensive place to learn it, and it took a real
    `make deploy-dev` to find. This is the repo's standing rule about fakes applied to the
    hermetic gate itself: when the service refuses something the gate accepted, teach the
    gate that one rule.

    Scans every synthesised template, not just this stack's, so the next metric filter
    added anywhere is covered on creation rather than on the deploy that fails.
    """
    from tests.unit.synthesised import templates

    offenders = [
        f"{name}:{logical}"
        for name, body in templates().items()
        for logical, resource in body.get("Resources", {}).items()
        if resource.get("Type") == "AWS::Logs::MetricFilter"
        for transformation in resource["Properties"]["MetricTransformations"]
        if transformation.get("Dimensions") and "DefaultValue" in transformation
    ]
    assert not offenders, (
        f"{offenders} set both Dimensions and DefaultValue on a metric transformation. "
        f"CloudWatch Logs rejects that pair with a 400 at deploy time. Keep the "
        f"dimensions — an alarm watching a dimension set nobody publishes can never fire "
        f"(V13-8) — and drop the default value."
    )


def test_the_stack_declares_no_log_group(template: Any) -> None:
    """Lambda creates `/aws/lambda/...` itself. A second declaration of the same name fails
    the deploy with "already exists" — after the change set has been accepted, which is the
    expensive place to learn it. The group is imported by name for exactly this reason."""
    template.resource_count_is("AWS::Logs::LogGroup", 0)


def test_the_dashboard_shows_every_metric_including_the_unalarmed_ones(template: Any) -> None:
    """A metric with no emitter renders as "no data", which is the honest picture of a
    control that is not built yet. Hiding it would make the dashboard's coverage look
    complete."""
    bodies = template.find_resources("AWS::CloudWatch::Dashboard")
    assert len(bodies) == 1
    rendered = str(next(iter(bodies.values()))["Properties"]["DashboardBody"])
    for spec in METRICS:
        assert spec.name in rendered, f"{spec.name} is on no dashboard widget"
