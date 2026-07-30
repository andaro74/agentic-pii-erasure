"""Alarms and a dashboard for ARCHITECTURE §10.1 — and nothing for a metric nobody emits.

**The rule this stack is built around: never create an alarm for a metric no code
publishes.** Such an alarm sits in `INSUFFICIENT_DATA` forever, renders as untriggered on
a dashboard, and so actively reports health it cannot observe. It is worse than a missing
alarm, because a missing alarm is visibly missing. `src/pii_erasure/observability/
metrics.py` records which module emits each metric, this stack alarms on exactly those,
and `tests/unit/test_observability_synth.py` asserts the two sets are equal — with
`NOT_ALARMED` naming a reason for every metric left out.

That is the third leg of the agreement the metric registry already holds with the
architecture doc. All three must say the same thing, and one of the three is a synthesised
CloudFormation template, so the check reads the template rather than the source.

**Missing data is not breaching.** Embedded metric format produces no data point when
nothing happens, and most of §10.1 is a counter whose healthy value is "silence". Left at
the CloudWatch default those alarms would never leave `INSUFFICIENT_DATA` — the same
failure as alarming on an unpublished metric, arrived at from the other direction. So every
counter alarm treats missing data as `NOT_BREACHING`, and the gauges do too: an eval that
did not run is not a recall failure, it is an eval that did not run.

**"M out of N" rather than a single datapoint.** AWS's guidance for EMF alarms on Lambda is
explicit that log flush timing is outside the caller's control, so a single-datapoint alarm
can fire on a partial window. Two datapoints out of two periods for the security-shaped
counters, which trades a period of latency for not paging somebody at 3am over a buffering
artefact.

**No SNS subscription is created here.** The topic exists so alarms have a destination; who
receives it is an operational decision, and a stack that mailed a hardcoded address on
deploy would be one nobody could deploy safely. `infra/README.md` records the subscribe
step.

**Cost.** Alarms are $0.10/month each and the dashboard is free below three per account;
neither is a provisioned floor, so ADR-021's rule holds. The metric filter is free. What is
*not* here for cost reasons is the CloudTrail data-event trail `dek_registry.read` needs —
see `NOT_ALARMED`.
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from pii_erasure.observability.metrics import METRICS, NAMESPACE, MetricSpec

#: Metrics this STACK publishes rather than application code. `saga.executor_timeout`
#: cannot be emitted in-process — a Lambda killed at its timeout runs no further code — so
#: it is extracted from the executor's log group by a metric filter below.
STACK_PUBLISHED = frozenset({"saga.executor_timeout"})

#: Metrics published with NO dimensions. The reason is the service's, not a preference.
#:
#: CloudWatch Logs only supports dimensions on a metric filter whose **filter pattern
#: extracts named fields** — a space-delimited `[a, b, c]` or JSON `{...}` pattern. Ours
#: matches the Lambda runtime's own timeout line, `Task timed out after N seconds`, which
#: is unstructured text and contains neither `stage` nor `plane`. Those two are constants
#: of the deployment, not fields of the event, so no pattern can produce them. Deploy
#: returns `The specified filter pattern does not support dimensions` (V13-12).
#:
#: **The consequence is disclosed rather than hidden: this metric does not separate by
#: stage.** Every stage's filter publishes into one `ASDP/Erasure saga.executor_timeout`,
#: so a `pr-1234` stack's executor timeout reaches the dev alarm. Every other metric here
#: carries `stage` and does separate. If that collision ever matters, the fix is the
#: alternative `MECHANISM_NOTES` already names — alarm on `AWS/Lambda` `Duration`
#: approaching the configured timeout, whose `FunctionName` dimension *is* stage-scoped —
#: and it costs the single-namespace property this registry is built on. Not worth it for
#: one metric today; written down so the trade is a decision rather than a discovery.
UNDIMENSIONED = frozenset({"saga.executor_timeout"})


def _dimensions_of(spec: MetricSpec, stage: str) -> dict[str, str]:
    """The dimension set an alarm or widget must watch for this metric.

    One place, so the alarm and the dashboard cannot disagree — and so the exception is
    visible as an exception rather than as a special case buried in two builders.
    """
    if spec.name in UNDIMENSIONED:
        return {}
    return {"stage": stage, "plane": _plane_of(spec)}


#: Every §10.1 metric that gets no alarm, and why. A test requires this to account for
#: exactly the difference between the registry and the alarms, so "we forgot one" cannot
#: masquerade as "we decided not to".
NOT_ALARMED: dict[str, str] = {
    "policy.deny": (
        "§10.1 asks for a spike, not a count: a deny is the policy layer working, and the "
        "adversarial corpus produces them by design. A static threshold would either page "
        "on normal operation or be set so high it never fires. Dashboarded so a human can "
        "see the shape; anomaly detection is the right control and is not free."
    ),
    "saga.duration": (
        "Emitted since `started_at` landed in SagaState — but with NO threshold, so no "
        "alarm. §10.1 asks for `> statutory deadline minus 7d` (23 days), and the default "
        "grace window is 30 (saga/nodes/plan.py), so a perfectly healthy saga reaches T+0 "
        "verification after the one-month deadline and would breach that threshold every "
        "single time. An always-red alarm is muted in week one, and a muted alarm is a "
        "missing alarm with extra steps. This is ARCHITECTURE §16 Q4 — the grace-window / "
        "statutory-deadline conflict — and the metric now measures it rather than "
        "restating it: dashboarded, so the number is visible while the question is open."
    ),
    "dek_registry.read": (
        "Requires CloudTrail DATA events on the DEK registry table: a read by a "
        "non-participant principal is invisible to the application. Not created here "
        "because a trail needs a bucket and bills per event, and switching it on is a "
        "deliberate cost decision rather than a default. Until it exists there is nothing "
        "to alarm on, and an alarm would report safety it cannot see — which for invariant "
        "14's threat (T9) is the worst possible lie."
    ),
}

#: One period for the fast-moving counters. Long enough that EMF's flush latency does not
#: split a single event across two windows.
_PERIOD = Duration.minutes(5)


def alarmed_metrics() -> tuple[MetricSpec, ...]:
    """The metrics that get an alarm: a threshold, and something that publishes them.

    Derived rather than listed, so adding an emitter is what creates an alarm and there is
    no second place to forget.
    """
    return tuple(
        spec
        for spec in METRICS
        if spec.threshold is not None and (spec.emitter or spec.name in STACK_PUBLISHED)
    )


class ObservabilityStack(Stack):
    """Alarms, the dashboard, and the one metric filter."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.stage = stage

        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"asdp-{stage}-alarms",
            display_name=f"ASDP {stage} erasure alarms",
        )

        # The executor's log group is IMPORTED, never declared. Lambda creates it on first
        # invocation, and a second stack declaring the same name fails the deploy with
        # "resource already exists" — after the change set has been accepted.
        self.executor_logs = logs.LogGroup.from_log_group_name(
            self,
            "SagaExecutorLogs",
            f"/aws/lambda/asdp-{stage}-saga-executor",
        )
        self._publish_executor_timeouts()

        self.alarms: dict[str, cloudwatch.Alarm] = {
            spec.name: self._alarm(spec) for spec in alarmed_metrics()
        }
        self._dashboard()

    # ── the one metric the stack publishes ───────────────────────────────────

    def _publish_executor_timeouts(self) -> None:
        """Extract `saga.executor_timeout` from the runtime's own timeout line.

        The Lambda runtime writes `Task timed out after N seconds` and then the process is
        gone, so nothing in `saga/` can record it. A phase that exceeds the ceiling is
        §3.2's known limit; the metric exists so it is observed rather than inferred from a
        saga that simply stopped moving.
        """
        logs.MetricFilter(
            self,
            "ExecutorTimeoutFilter",
            log_group=self.executor_logs,
            filter_name=f"asdp-{self.stage}-executor-timeout",
            # A quoted string term matches the literal substring anywhere in the event.
            filter_pattern=logs.FilterPattern.literal('"Task timed out after"'),
            metric_namespace=NAMESPACE,
            metric_name="saga.executor_timeout",
            metric_value="1",
            # NO dimensions — see UNDIMENSIONED. Two deploys, two different 400s, and the
            # pair of them is the whole lesson: `cdk synth` validates template shape
            # against the resource schema and never the service's semantics, so both were
            # green in the hermetic gate.
            #
            #   1. dimensions + default_value together → "mutually exclusive" (V13-11)
            #   2. dimensions on a literal filter pattern → "the specified filter pattern
            #      does not support dimensions" (V13-12)
            #
            # The second is the one that settles it. Dimensions require a pattern that
            # extracts named FIELDS, and `stage`/`plane` are constants of the deployment
            # rather than fields of the log line — no pattern can conjure them. So the
            # dimensions go, and with them gone `default_value` becomes legal again.
            #
            # Which is a genuine consolation rather than a shrug: the zero baseline is
            # what lets the alarm tell "no timeouts" from "nothing reported", and it is
            # the reason `_alarm` can leave this one on its normal footing.
            default_value=0,
        )

    # ── alarms ───────────────────────────────────────────────────────────────

    def _alarm(self, spec: MetricSpec) -> cloudwatch.Alarm:
        assert spec.threshold is not None  # alarmed_metrics() filters these out
        below = spec.alarm_when == "below"
        # Security-shaped counters get two datapoints; a recall gauge gets one, because it
        # is written once per eval run and waiting for a second would mean waiting for the
        # next deploy.
        datapoints = 1 if spec.statistic in ("Minimum", "Maximum", "p99") else 2
        alarm = cloudwatch.Alarm(
            self,
            f"Alarm{_construct_id(spec.name)}",
            alarm_name=f"asdp-{self.stage}-{spec.name}",
            alarm_description=spec.why,
            metric=cloudwatch.Metric(
                namespace=NAMESPACE,
                metric_name=spec.name,
                # The BASE dimension set, which `Dimensions.dimension_sets()` publishes
                # unconditionally — or none at all for the metric the log-filter publishes
                # bare. A dimension set is part of a metric's identity and nothing rolls up
                # across them, so alarming on `{stage, plane, systemId}` would be correct
                # only for the call sites that pass a participant, and alarming on
                # `{stage, plane}` is wrong for a metric published without either. Before
                # V13-8 the emitter published only the wide set and this alarm watched a
                # combination nothing wrote; V13-12 is the same mistake from the stack's
                # side. One helper answers it for both builders.
                dimensions_map=_dimensions_of(spec, self.stage),
                statistic=spec.statistic,
                period=_PERIOD,
            ),
            threshold=spec.threshold,
            comparison_operator=(
                cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD
                if below
                else cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
            ),
            evaluation_periods=datapoints,
            datapoints_to_alarm=datapoints,
            # EMF is sparse. The default would leave every one of these in
            # INSUFFICIENT_DATA forever, which looks identical to "watching nothing".
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alarm_topic))
        return alarm

    # ── dashboard ────────────────────────────────────────────────────────────

    def _dashboard(self) -> None:
        """Every §10.1 metric, including the ones with no alarm.

        Deliberately wider than the alarm set: `policy.deny` has no sensible static
        threshold but its *shape* is the injection signal, and a metric with no emitter
        shows as "no data", which is the honest rendering of a control that is not built
        yet rather than a green tick.
        """
        cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"asdp-{self.stage}-erasure",
            widgets=[
                [
                    cloudwatch.GraphWidget(
                        title=f"{spec.name} — {spec.why}",
                        left=[
                            cloudwatch.Metric(
                                namespace=NAMESPACE,
                                metric_name=spec.name,
                                dimensions_map=_dimensions_of(spec, self.stage),
                                statistic=spec.statistic,
                                period=_PERIOD,
                            )
                        ],
                        width=12,
                    )
                ]
                for spec in METRICS
            ],
        )


def _plane_of(spec: MetricSpec) -> str:
    """The `plane` dimension the emitter actually uses.

    Wrong here and the alarm watches a metric nobody writes — the dimension is part of the
    metric's identity in CloudWatch, so `plane=saga` and `plane=resume` are two different
    metrics with the same name. Derived from where the emitter lives so the two cannot
    drift silently.
    """
    emitter = spec.emitter or ""
    if emitter.startswith("scheduler."):
        return "resume"
    if emitter.startswith("evals"):
        return "eval"
    if emitter.startswith("policy."):
        return "policy"
    return "saga"


def _construct_id(metric_name: str) -> str:
    """`saga.executor_timeout` → `SagaExecutorTimeout`. CDK ids are alphanumeric."""
    return "".join(part.title() for part in metric_name.replace(".", "_").split("_"))
