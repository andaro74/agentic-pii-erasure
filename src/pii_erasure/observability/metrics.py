"""The metrics of ARCHITECTURE §10.1, emitted as CloudWatch Embedded Metric Format.

**One registry, three consumers.** `METRICS` below is the single definition of what this
platform measures. ARCHITECTURE §10.1 documents it, the emitters here publish it, and
`infra/stacks/observability.py` alarms on it — and a test asserts all three agree. An alarm
on a metric nothing publishes is not a weak control, it is an *anti*-control: it renders
green on a dashboard and can never fire.

**Why EMF and not `PutMetricData`.** EMF is a structured log line that CloudWatch Logs
extracts metrics from, so it needs no `cloudwatch:PutMetricData` grant, adds no API call to
a saga node's critical path, and cannot fail independently of the log write. The saga's IAM
stays as narrow as invariant 12 demands. The cost is that metrics are **sparse** — no data
point is produced when nothing happens — which is why every alarm on a counter here must
treat missing data as *not breaching* rather than sitting in INSUFFICIENT_DATA.

**Dimensions stay low-cardinality, and the two reasons agree.** EMF creates one custom
metric per unique dimension combination, so a `subjectRef` dimension would bill per subject
*and* write a subject identifier into a metrics surface (invariant 5). Cost and privacy
forbid the same thing here, so `emit` rejects a suspect dimension rather than scrubbing it —
the same posture as the AgentCore Memory pre-write path (invariant 13).

That rejection is what makes `observability/redact.py`'s exemption of the `_aws` node safe:
the node is validated where it is *built*, loudly, before it can reach a log processor
where raising would break logging (V13-3).

Framework-free and boto3-free: an EMF document is JSON on stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pii_erasure.observability.logging import get_logger
from pii_erasure.observability.redact import reject_if_pii

#: The CloudWatch namespace. One per platform, not per stage — stage is a dimension, so a
#: single alarm definition works across stages and a dashboard can compare them.
NAMESPACE = "ASDP/Erasure"

Unit = Literal["Count", "Seconds", "Milliseconds", "Percent", "None"]

#: Statistics an alarm may use, kept here so the stack cannot invent one that makes no
#: sense for the metric's type (a p90 of a counter, say).
Statistic = Literal["Sum", "Average", "Maximum", "Minimum", "p90", "p99"]

#: Which side of the threshold is the bad side. Part of what §10.1's "Alarm" column
#: means, so it lives with the threshold rather than in the stack: `discovery.recall`
#: is the one metric where LOW is the failure, and an alarm built with the operator
#: reversed would be permanently green on the only number ADR-008 calls P1.
AlarmWhen = Literal["above", "below"]


@dataclass(frozen=True)
class MetricSpec:
    """One row of ARCHITECTURE §10.1, in a form the code and the stack both read."""

    name: str
    unit: Unit
    #: What the row's "Alarm" column means, as a machine-readable threshold. `None` means
    #: the metric is tracked and dashboarded but deliberately not alarmed — `policy.deny`
    #: is the case: a spike is interesting, a single deny is normal operation.
    threshold: float | None
    statistic: Statistic
    #: True when the platform's own code emits it. The two that are False cannot be
    #: emitted by application code and say why — see MECHANISM_NOTES.
    emitted_by_app: bool
    why: str
    #: The module that calls `emit` for this metric, dotted and relative to
    #: `pii_erasure` (or an `evals.` path). `None` means no call site yet, and a test
    #: requires such a metric to appear in MECHANISM_NOTES or PENDING_EMITTERS — so
    #: "documented, alarmed, and emitted by nobody" is a build failure rather than a
    #: dashboard that reads healthy.
    emitter: str | None = None
    alarm_when: AlarmWhen = "above"


#: Twelve rows, in §10.1's order. Kept as data so drift between the doc, the emitters and
#: the alarms is a test failure rather than a reading exercise.
METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "discovery.recall",
        "Percent",
        1.0,
        "Minimum",
        True,
        "Any value below 1.00 is P1 — the one metric a false negative hides behind",
        "evals.run",
        "below",
    ),
    MetricSpec(
        "deletion.residual_artifacts",
        "Count",
        0,
        "Sum",
        True,
        "Anything remaining after verify that no participant disclosed",
        "saga.nodes.verify",
    ),
    MetricSpec(
        "resurrection.detected",
        "Count",
        0,
        "Sum",
        True,
        "A write path that bypasses the tombstone check — distinct from a deletion failure",
        "saga.nodes.sweep",
    ),
    MetricSpec(
        "approval.time_to_decision",
        "Seconds",
        # 20 days, and the approval timeout is 14 (`saga/deps.py`) — so under working
        # conditions this alarm is unreachable, because silence becomes a DENY decision
        # six days before the threshold. That is deliberate rather than dead: the only way
        # past 20 days is a timeout wake that never fired, which is precisely the failure
        # a saga cannot report about itself. Unlike `saga.duration` below, the healthy case
        # sits well *under* the threshold, so the alarm is quiet rather than always-red.
        20 * 86400,
        "p99",
        True,
        "p90 SLO is 5 days; alarm at p99 > 20d, before the statutory deadline",
        "saga.nodes.approval_gate",
    ),
    MetricSpec(
        "phase3.stuck_participants",
        "Count",
        0,
        "Maximum",
        True,
        "A halted erasure needs a human; 24h of it is a missed deadline forming",
        "saga.nodes.hard_delete",
    ),
    MetricSpec(
        "policy.deny",
        "Count",
        None,
        "Sum",
        True,
        "Not alarmed on absolute count — a spike means injection or misconfiguration",
        "policy.engine",
    ),
    MetricSpec(
        "manifest.digest_mismatch",
        "Count",
        0,
        "Sum",
        True,
        "Any occurrence is a security event (§8.3), not a retry",
        "saga.nodes.hard_delete",
    ),
    MetricSpec(
        "saga.duration",
        "Seconds",
        # §10.1 specifies `> statutory deadline minus 7d` — 23 days. Deliberately NOT set:
        # the default grace window is 30 days (`saga/nodes/plan.py`), so a healthy saga
        # reaches T+0 verification *after* the one-month deadline and every single one of
        # them would breach a 23-day threshold. That is ARCHITECTURE §16 Q4 — the
        # grace-window/deadline conflict — showing up as a number instead of a caveat.
        # Setting the threshold anyway would produce an always-red alarm, which gets
        # muted in week one and is then indistinguishable from no alarm at all.
        None,
        "Maximum",
        True,
        "Intake to T+0 verification. Measures §16 Q4's conflict; unalarmed until it is answered",
        "saga.nodes.verify",
    ),
    MetricSpec(
        "checkpoint.resume_failure",
        "Count",
        0,
        "Sum",
        True,
        "An upgrade defect, not a participant defect — ADR-016's stranded saga",
        "scheduler.handler",
    ),
    MetricSpec(
        "scheduler.duplicate_wake",
        "Count",
        0,
        "Sum",
        True,
        "Resume idempotency leaking (invariant 11)",
        "scheduler.handler",
    ),
    MetricSpec(
        "saga.executor_timeout",
        "Count",
        0,
        "Sum",
        False,
        "A Lambda that times out cannot log its own timeout — see MECHANISM_NOTES",
    ),
    MetricSpec(
        "dek_registry.read",
        "Count",
        0,
        "Sum",
        False,
        "A non-participant principal reading wrapped DEKs — CloudTrail, not app code",
    ),
)

BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in METRICS}

#: Metrics whose emitter is not written yet, and what is actually blocking each. Separate
#: from MECHANISM_NOTES because these *can* be emitted by application code — they are
#: waiting on a decision, not on a different mechanism. A test requires every unwired
#: metric to appear in one dict or the other, so "documented, alarmed, emitted by nobody"
#: cannot pass quietly.
#:
#: **Currently empty, and that is the point** rather than a sign the dict is dead. Both
#: entries it held
#: — the two duration metrics — were waiting on `started_at` in `SagaState`, which now
#: exists; the mechanism stays so the *next* metric added without an emitter fails the
#: build instead of joining the dashboard as a permanent "no data" tile.
PENDING_EMITTERS: dict[str, str] = {}

#: The two metrics application code cannot emit, and what must publish them instead.
#: Written down because §10.1 tabulates all twelve as if they were alike, and building
#: emitters for these two would have produced code that never runs.
MECHANISM_NOTES: dict[str, str] = {
    "saga.executor_timeout": (
        "A Lambda killed at its timeout runs no more code, so nothing in-process can "
        "record it. Must come from a CloudWatch Logs metric filter on the runtime's "
        "'Task timed out after' line, or from the Lambda Duration metric approaching the "
        "configured timeout. The stack owns this one, not the saga."
    ),
    "dek_registry.read": (
        "Reads by a non-participant principal are only visible in CloudTrail data events "
        "for the DEK registry table — the application never sees the call it is trying to "
        "detect. Invariant 14's neighbour: the registry's protection is what is NOT "
        "configured, and the detection is likewise outside the app."
    ),
}


@dataclass(frozen=True)
class Dimensions:
    """The dimension set. Deliberately a closed shape rather than free kwargs.

    Every field is low-cardinality by construction. `subjectRef` is absent and cannot be
    added by a caller, which is the cheapest possible enforcement of both the EMF cost
    warning and invariant 5.
    """

    stage: str
    #: The plane emitting the metric: `saga`, `resume`, `runtime`, `eval`, `participant`.
    plane: str
    #: Optional participant id, for the metrics that are per-system.
    system_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"stage": self.stage, "plane": self.plane}
        if self.system_id:
            out["systemId"] = self.system_id
        return out

    def dimension_sets(self) -> list[list[str]]:
        """The dimension **sets** to publish under — always including the base set.

        A dimension set is part of a metric's identity in CloudWatch, not an annotation on
        it: `{stage, plane}` and `{stage, plane, systemId}` are two different metrics that
        happen to share a name. EMF creates a metric for each set listed here and **no
        rollup across them**, so publishing only the wide set means an alarm written
        against the narrow one never receives a data point — it sits in
        `INSUFFICIENT_DATA`, or, once `TreatMissingData` is `NOT_BREACHING` as every alarm
        here sets it, renders permanently green (V13-8).

        So `{plane, stage}` is emitted unconditionally and the wider set is emitted
        *as well* when there is an optional dimension. The alarms and the dashboard are
        written against the base set — one shape, whatever the call site passes — and the
        wide set answers the operator's first question, *which system*.

        The cost is one extra custom metric per unique combination, and the combinations
        stay bounded because `Dimensions` is a closed shape over low-cardinality fields.
        """
        base = ["plane", "stage"]
        wide = sorted(self.as_dict())
        return [base] if wide == base else [base, wide]


class MetricError(RuntimeError):
    """The metric or its dimensions are not something this platform measures.

    Loud on purpose. A typo'd metric name would publish a metric no alarm watches, which
    reads exactly like a healthy system.
    """


def emf_document(
    name: str,
    value: float,
    dimensions: Dimensions,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one EMF document. Pure — the caller logs it.

    Separated from `emit` so the shape is unit-testable against the published spec
    without capturing log output.
    """
    spec = BY_NAME.get(name)
    if spec is None:
        raise MetricError(
            f"{name!r} is not in ARCHITECTURE §10.1. Add a MetricSpec — and an alarm — "
            f"rather than publishing a metric nothing watches."
        )

    dims = dimensions.as_dict()
    for key, dim_value in dims.items():
        # Loud, not sanitising: a PII-shaped dimension is a bug in the caller, and
        # scrubbing it would leave a metric silently split across a redacted value.
        reject_if_pii(key)
        reject_if_pii(dim_value)

    moment = now or datetime.now(timezone.utc)
    return {
        "_aws": {
            # Milliseconds since the epoch, per the specification.
            "Timestamp": int(moment.timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    # An array of dimension SETS; each set is an array of member names
                    # that must also exist on the root node. The base set is always one of
                    # them — see `Dimensions.dimension_sets`.
                    "Dimensions": dimensions.dimension_sets(),
                    "Metrics": [{"Name": spec.name, "Unit": spec.unit}],
                }
            ],
        },
        **dims,
        spec.name: value,
    }


def emit(name: str, value: float, dimensions: Dimensions, **context: Any) -> None:
    """Publish one metric, and whatever context helps a human read the log beside it.

    `context` is scrubbed by the logging processor like any other field; only the `_aws`
    node is exempt, and it is validated above rather than trusted.
    """
    document = emf_document(name, value, dimensions)
    get_logger("pii_erasure.metrics").info("metric", **document, **context)
