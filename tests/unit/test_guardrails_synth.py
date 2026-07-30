"""The account-level cost guardrails, and the property that keeps CI from deleting them.

The positive half — two budgets, the right alert types, a topic policy Budgets can actually
use — is the easy half. **The half that matters is negative: no stage deploy may contain a
budget.** `make deploy-dev` runs `cdk deploy --all` and `make destroy-dev` runs
`cdk destroy --all`, and CI runs both against an ephemeral `pr-<run_id>` stage. A budget
reachable from `--all` would be created per pull request and, on teardown, **deleted** —
the account's cost guardrail removed by a routine green build.

That is asserted against the synthesised templates in `infra/cdk.out`, which is what
`make deploy-dev` would actually hand to CloudFormation, rather than against the source.
"""

from __future__ import annotations

import sys
from itertools import takewhile
from pathlib import Path
from typing import Any

import pytest

from tests.unit.synthesised import SYNTH, templates

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "infra"))

from stacks.guardrails import (  # noqa: E402 — needs the path insert above
    DEFAULT_DAILY_USD,
    DEFAULT_MONTHLY_USD,
    STACK_NAME,
    AccountGuardrailsStack,
)

BUDGET = "AWS::Budgets::Budget"


@pytest.fixture(scope="module")
def template() -> Any:
    from aws_cdk import App, assertions

    app = App(context={"accountGuardrails": "true"})
    return assertions.Template.from_stack(AccountGuardrailsStack(app, STACK_NAME))


def _budgets(template: Any) -> dict[str, dict[str, Any]]:
    return {
        body["Properties"]["Budget"]["BudgetName"]: body["Properties"]
        for body in template.find_resources(BUDGET).values()
    }


# ─── the negative property ────────────────────────────────────────────────────────────


def test_nothing_the_default_app_synthesises_contains_a_budget() -> None:
    """The reason this stack exists separately at all.

    A budget is account-wide. Everything the default app synthesises is inside
    `cdk deploy --all` — and therefore inside `cdk destroy --all`, which CI runs on every
    pull request. So a budget anywhere in that set would be created per PR and **deleted**
    on teardown: a green build would silently disarm the account's cost guardrail.

    The assertion is over the whole synthesised set rather than over "stage stacks",
    because that is the actual property. Anything `--all` can create, `--all` can destroy.
    """
    offenders = []
    for name, body in templates().items():
        for logical, resource in body.get("Resources", {}).items():
            if resource.get("Type", "").startswith("AWS::Budgets::"):
                offenders.append(f"{name}:{logical}")
    assert not offenders, (
        f"{offenders} puts an account-wide budget inside `cdk deploy --all`, which means "
        f"`make destroy-dev` deletes it — and CI runs that on every pull request."
    )


def test_the_guardrails_stack_is_not_synthesised_by_default() -> None:
    """The mechanism behind the assertion above: `app.py` builds this stack only behind the
    `accountGuardrails` context flag, so neither `--all` verb can reach it. If the flag
    were dropped, the template would appear here and `make destroy-dev` could delete it."""
    assert not (SYNTH / f"{STACK_NAME}.template.json").exists(), (
        f"{STACK_NAME} synthesised without its context flag — it is now inside "
        f"`cdk deploy --all` and `cdk destroy --all`"
    )


def test_the_stack_name_carries_no_stage() -> None:
    """A stage-suffixed guardrail is one `make destroy-dev` away from not existing, and the
    name is what a human reads when deciding whether a stack is safe to delete."""
    assert STACK_NAME == "asdp-account-guardrails"
    for stage in ("dev", "prod", "pr-123"):
        assert stage not in STACK_NAME


def test_the_makefile_deploys_this_stack_by_name_and_not_with_all() -> None:
    """`--all` is what makes a stack ephemeral here. The target must name the stack, pass
    the context flag, and never use `--all`; and the name must match the module's."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert f"GUARDRAILS_STACK := {STACK_NAME}" in makefile, (
        "the Makefile and stacks/guardrails.py disagree about the stack name"
    )
    # The RECIPE only — the tab-indented lines make deploys. Prose below the target
    # discusses `--all` at length, and matching that would have made this check pass or
    # fail on a comment rather than on what runs.
    body = makefile.split("deploy-guardrails:", 1)[1]
    target = "\n".join(takewhile(lambda line: line.startswith("\t"), body.splitlines()[1:]))
    assert target.strip(), "deploy-guardrails has no recipe at all"
    assert "$(GUARDRAILS_STACK)" in target, "deploy-guardrails does not name the stack"
    assert "--context accountGuardrails=true" in target, "the context flag is not passed"
    assert "--all" not in target, (
        "deploy-guardrails uses --all, which would drag the whole stage stack along and "
        "put the budget back inside the destroy path"
    )


# ─── the budgets themselves ───────────────────────────────────────────────────────────


def test_two_budgets_answer_two_different_questions(template: Any) -> None:
    """A MONTHLY budget catches an overrun; a DAILY one catches a leaked stack. Only the
    second detects the failure this architecture is actually exposed to — an idle stack
    costs cents, so anything above a few dollars in a day is something still running."""
    found = _budgets(template)
    assert set(found) == {"asdp-account-monthly", "asdp-account-daily"}

    monthly = found["asdp-account-monthly"]["Budget"]
    assert monthly["TimeUnit"] == "MONTHLY"
    assert monthly["BudgetType"] == "COST"
    assert monthly["BudgetLimit"] == {"Amount": DEFAULT_MONTHLY_USD, "Unit": "USD"}

    daily = found["asdp-account-daily"]["Budget"]
    assert daily["TimeUnit"] == "DAILY"
    assert daily["BudgetLimit"] == {"Amount": DEFAULT_DAILY_USD, "Unit": "USD"}


def test_the_monthly_budget_warns_before_the_money_is_gone(template: Any) -> None:
    """An ACTUAL-only alert arrives after the spend. A FORECASTED alert is the only one
    that fires while there is still something to prevent, which is the entire point of a
    budget over a bill."""
    notifications = _budgets(template)["asdp-account-monthly"]["NotificationsWithSubscribers"]
    kinds = {n["Notification"]["NotificationType"] for n in notifications}
    assert "FORECASTED" in kinds, "the monthly budget can only report an overrun after it"
    assert "ACTUAL" in kinds


def test_no_budget_takes_an_action(template: Any) -> None:
    """Two reasons, and either alone is sufficient. Action-enabled budgets bill $0.10/day
    beyond the first two, where monitoring is free. And an action that could stop resources
    to save money is a mechanism for interrupting an in-flight erasure or its audit trail —
    a cost control with the power to cause the breach it is saving money against."""
    template.resource_count_is("AWS::Budgets::BudgetsAction", 0)


def test_no_budget_is_filtered_by_a_tag(template: Any) -> None:
    """A tag filter would report zero forever unless somebody activated the cost-allocation
    tag by hand in the Billing console — AWS: *"You must activate tags to use them."* That
    is a control that cannot fire, dressed as a scoped budget."""
    for name, properties in _budgets(template).items():
        assert "CostFilters" not in properties["Budget"], (
            f"{name} carries a cost filter; if it is a tag filter it silently matches "
            f"nothing until a manual Billing console step is done"
        )


# ─── the permission the alerts do not arrive without ──────────────────────────────────


def _topic_policy(template: Any) -> dict[str, Any]:
    bodies = template.find_resources("AWS::SNS::TopicPolicy")
    assert len(bodies) == 1, "expected exactly one topic policy"
    return next(iter(bodies.values()))["Properties"]["PolicyDocument"]


def test_budgets_may_publish_to_the_alert_topic(template: Any) -> None:
    """Budgets publishes as a service principal, so the topic must allow it. Without this
    the budget fails to create with "Invalid SNS topic" — the alert would be configured and
    undeliverable."""
    statements = [
        s
        for s in _topic_policy(template)["Statement"]
        if s.get("Principal", {}).get("Service") == "budgets.amazonaws.com"
    ]
    assert statements, "budgets.amazonaws.com cannot publish to the alert topic"
    assert statements[0]["Action"] == "SNS:Publish"


def test_the_publish_grant_is_confused_deputy_scoped(template: Any) -> None:
    """`budgets.amazonaws.com` is every account's budget service, not just ours. Both
    conditions are AWS's documented pair, and without them this topic is a target any
    other account's budgets could publish to."""
    statement = next(
        s
        for s in _topic_policy(template)["Statement"]
        if s.get("Principal", {}).get("Service") == "budgets.amazonaws.com"
    )
    conditions = statement["Condition"]
    assert "aws:SourceAccount" in conditions["StringEquals"]
    assert "aws:SourceArn" in conditions["ArnLike"]


def test_the_alert_topic_is_not_encrypted(template: Any) -> None:
    """Deliberate, and it must stay deliberate. AWS Budgets cannot publish to an SSE topic
    without extra KMS grants, and the Budgets troubleshooting guide's own remedy is to
    disable encryption. Adding a KMS key here would stop the alerts silently — no budget
    alert carries subject data, so invariant 5 is not what is being traded away."""
    bodies = template.find_resources("AWS::SNS::Topic")
    assert len(bodies) == 1
    properties = next(iter(bodies.values()))["Properties"]
    assert "KmsMasterKeyId" not in properties, (
        "the budget alert topic is encrypted; Budgets will refuse to publish to it"
    )


def test_no_email_address_is_baked_into_the_template(template: Any) -> None:
    """A hardcoded address is a stack nobody else can deploy, and one whose author receives
    a stranger's cost alerts forever. Every subscriber is the SNS topic; who reads it is an
    operational decision, recorded in infra/README.md."""
    for name, properties in _budgets(template).items():
        for notification in properties["NotificationsWithSubscribers"]:
            for subscriber in notification["Subscribers"]:
                assert subscriber["SubscriptionType"] == "SNS", (
                    f"{name} notifies an address written into the template"
                )
