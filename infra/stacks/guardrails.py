"""Account-level cost guardrails — budgets, and the one thing that must never be ephemeral.

**A budget is account-wide, and this repo's CI creates and destroys a stack per pull
request.** `make deploy-dev` runs `cdk deploy --all` and `make destroy-dev` runs
`cdk destroy --all`, both against a per-run stage (`pr-<run_id>`). A budget living in any
stage-scoped stack would therefore be created N times over — N identical alerts on the same
account total, each labelled with a stage that did not incur it — and, far worse, **every
PR teardown would delete the account's cost guardrail.** A control that a routine CI run
removes is not a control.

So this stack is **not stage-scoped and not instantiated by default.** `infra/app.py` builds
it only when the `accountGuardrails` context flag is set, which keeps it out of both
`--all` verbs. A human deploys it once per account:

```bash
make deploy-guardrails      # human-only, denied to Claude Code
```

**Why not scope a budget to a stage with a tag filter instead?** Because the filter would
silently match nothing. AWS's own documentation is explicit — *"Tag: If you activated any
tags, choose a resource tag… You must activate tags to use them."* Activating a
cost-allocation tag is a manual step in the Billing console, outside CloudFormation and
outside this repo. A per-stage tag-filtered budget would deploy cleanly, report $0.00
forever, and read exactly like an account that is not spending money. That is the same
failure class as an alarm on an unpublished metric (see `observability.py`), reached from
the billing side.

**Two budgets, two different questions.** A MONTHLY budget answers *"is this month going to
overrun?"* and gets a FORECASTED alert, because a forecast is the only notification that
arrives while the spend can still be prevented. A DAILY budget answers *"did somebody leave
a stack up?"* — the one failure this architecture is actually exposed to, since an idle
stack costs cents and a leaked one with Bedrock traffic does not. Its alert is ACTUAL: a
one-day forecast on a spiky serverless bill is noise, whereas a few dollars crossed in a
single day is the signal.

**Cost: none.** AWS's pricing page: *"You can monitor and receive notifications on your
budgets free of charge."* Only *action-enabled* budgets bill ($0.10/day after the first
two), and these take no actions — deliberately. A budget action that could stop resources
would be a mechanism for deleting the audit trail of an in-flight erasure to save money.

**The SNS topic is deliberately unencrypted.** AWS Budgets cannot publish to an SSE topic
without additional KMS grants, and the Budgets troubleshooting guide's own remedy is
"disable encryption on the topic". Budget alerts carry a dollar amount and an account id —
no subject data ever reaches them (invariant 5 is not in play here) — so the trade is a
notification that works against a control that does not. Encrypting this topic later would
silently stop the alerts.

**No subscription is created.** Same reasoning as the alarm topic: who gets paged is an
operational decision, and Budgets' SNS subscribers must confirm by email anyway.
`infra/README.md` records the subscribe step.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from constructs import Construct

#: The stack's name, with no stage in it — the property this whole module exists to hold.
#: A test asserts the absence, because a stage-suffixed guardrail is one `make destroy-dev`
#: away from not existing.
STACK_NAME = "asdp-account-guardrails"

#: Monthly ceiling in USD. Generous enough that a working month does not page anybody, low
#: enough that a runaway is caught in days. Overridable: `--context monthlyBudgetUsd=250`.
DEFAULT_MONTHLY_USD = 100

#: Daily ceiling in USD. An idle stack costs cents a month, so a day above this means
#: something is running that should not be — the leaked-stack signal.
DEFAULT_DAILY_USD = 5


class AccountGuardrailsStack(Stack):
    """Budgets and their alert topic. Account-scoped, deployed once, never by CI."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        monthly_usd: int = DEFAULT_MONTHLY_USD,
        daily_usd: int = DEFAULT_DAILY_USD,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "BudgetAlerts",
            topic_name="asdp-account-budget-alerts",
            display_name="ASDP account cost guardrails",
            # No `master_key`: see the module docstring. Budgets refuses an encrypted topic.
        )
        self._allow_budgets_to_publish()

        # Names are unique per account (AWS: "Your budget name must be unique within your
        # account"), which means a second deploy of this stack into another region fails
        # rather than quietly creating a duplicate account-wide budget. That collision is a
        # feature here, so the names are fixed rather than made unique per stack.
        self.monthly = self._budget(
            "Monthly",
            name="asdp-account-monthly",
            time_unit="MONTHLY",
            amount=monthly_usd,
            notifications=(
                # 80% of the month gone: informational, and the number a human can act on.
                ("ACTUAL", 80.0),
                # The one that arrives while the money can still be saved.
                ("FORECASTED", 100.0),
            ),
        )
        self.daily = self._budget(
            "Daily",
            name="asdp-account-daily",
            time_unit="DAILY",
            amount=daily_usd,
            # ACTUAL only — a one-day forecast on a serverless bill is noise.
            notifications=(("ACTUAL", 100.0),),
        )

        CfnOutput(
            self,
            "BudgetAlertTopicArn",
            value=self.topic.topic_arn,
            description=(
                "Subscribe an operator to this topic. Nothing is subscribed by the stack, "
                "and an unsubscribed budget alert goes nowhere."
            ),
        )

    # ── the permission without which the alerts silently do not arrive ───────

    def _allow_budgets_to_publish(self) -> None:
        """Budgets publishes as a service principal, so the TOPIC has to allow it.

        Without this statement the budget fails to create with "Invalid SNS topic" — or, on
        an older topic, simply never delivers. Both conditions are from AWS's documented
        policy rather than invented: `aws:SourceAccount` and an `aws:SourceArn` of
        `arn:aws:budgets::<account>:*` (note the empty region — Budgets is global). They
        are what stop this topic from being a confused-deputy target for another account's
        budget service.
        """
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AWSBudgetsSNSPublishingPermissions",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["SNS:Publish"],
                resources=[self.topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:budgets::{self.account}:*"},
                },
            )
        )

    # ── budgets ──────────────────────────────────────────────────────────────

    def _budget(
        self,
        construct_id: str,
        *,
        name: str,
        time_unit: str,
        amount: int,
        notifications: tuple[tuple[str, float], ...],
    ) -> budgets.CfnBudget:
        return budgets.CfnBudget(
            self,
            construct_id,
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=name,
                budget_type="COST",
                time_unit=time_unit,
                budget_limit=budgets.CfnBudget.SpendProperty(amount=amount, unit="USD"),
                # No TimePeriod: it defaults to the current period. A hardcoded start date
                # in a template is a date somebody has to remember to move.
                #
                # No CostFilters either — this is the whole account on purpose. See the
                # module docstring on why a tag filter would report zero forever.
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=notification_type,
                        comparison_operator="GREATER_THAN",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    # SNS rather than an email address. A hardcoded address is a stack
                    # nobody else can deploy, and one this repo's author would receive
                    # alerts from forever.
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS",
                            address=self.topic.topic_arn,
                        )
                    ],
                )
                for notification_type, threshold in notifications
            ],
        )
