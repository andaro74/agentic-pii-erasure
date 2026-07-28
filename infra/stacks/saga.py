"""Saga stack: the executor + resume Lambdas, the SQS DLQ, and the Scheduler role.

The stateful primitives (checkpoints, ledger, tombstones, idempotency, the signing
CMK) live in the foundation stack; this stack is pure compute and wiring.

**What is withheld is the point** (invariant 12): neither Lambda role carries any
``bedrock:*`` action — the saga replays approved manifests and never talks to a model.
That used to be a code-review rule backed by an import test; here it is also an IAM
fact, asserted at synth time in `tests/unit/test_saga_synth.py`. Neither function
attaches to a VPC (ADR-023's rule), and the participant-invoke grant names exactly the
eight participant functions rather than a wildcard — the executor can reach the
participants and nothing else.

Two deliberate ARN constructions avoid CFN reference cycles:

* the resume function's own ARN goes into both environments as a *formatted string*
  (its function name is deterministic), because the resume Lambda schedules wakes
  that target itself;
* the Scheduler role's invoke policy names the resume function by formatted ARN for
  the same reason — the role's ARN is in the Lambda environment, so a live reference
  from role policy back to the function would close a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

#: Staged by `make package` alongside the participants asset. Committed empty with a
#: .gitkeep so `cdk synth` stays hermetic (no build step, no Docker).
SAGA_ASSET = str(Path(__file__).resolve().parents[1] / "build" / "saga")

_RUNTIME = lambda_.Runtime.PYTHON_3_12

#: Dev stages compress the wall-clock timers so `make integration` and `make walkthrough`
#: complete in minutes: sweeps at T+2min/T+4min instead of T+7d/T+30d, approval timeout at
#: one hour instead of 14 days. The *sequence* is identical — only the delays shrink, and
#: the scheduler still fires. Bypassing it would exercise a path that does not exist in
#: production, which is the difference between a demonstration and a simulation.
#:
#: Read from the environment because `.env.example` has promised exactly that since M0
#: ("Compress via this parameter only") and both named milestones then landed without
#: wiring it — the same drift ADR-025 left in ADR-015, caught here by checking every ⏳
#: marker as M8 closed. The defaults are what the constants used to be, so an unset
#: environment deploys the behaviour that was already deployed.
_DEV_SWEEP_DELAYS = os.environ.get("PII_ERASURE_SWEEP_DELAYS_SECONDS", "120,240")
_DEV_APPROVAL_TIMEOUT = os.environ.get("PII_ERASURE_APPROVAL_TIMEOUT_SECONDS", "3600")
_DEV_GRACE_WINDOW = os.environ.get("PII_ERASURE_GRACE_WINDOW_SECONDS", "120")


class SagaStack(Stack):
    """The execution plane. Deterministic replay; no model, no Bedrock, no VPC."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        checkpoints: dynamodb.ITable,
        checkpoint_offload: s3.IBucket,
        ledger: dynamodb.ITable,
        tombstones: dynamodb.ITable,
        idempotency: dynamodb.ITable,
        signing_key: kms.IKey,
        participants: dict[str, lambda_.IFunction],
        discovery_runtime_arn: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]
        prod = stage == "prod"

        # ── The phase-3 halt signal (§5): forward recovery ran out of road ───
        self.dlq = sqs.Queue(
            self,
            "SagaDlq",
            queue_name=f"asdp-{stage}-saga-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        resume_arn = self.format_arn(
            service="lambda",
            resource="function",
            resource_name=f"asdp-{stage}-saga-resume",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

        # ── Scheduler execution role: may invoke the resume Lambda, full stop ─
        self.scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"asdp-{stage}-scheduler",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="EventBridge Scheduler -> resume Lambda, nothing else",
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[resume_arn])
        )
        self.dlq.grant_send_messages(self.scheduler_role)

        environment = {
            "PII_ERASURE_STAGE": stage,
            "CHECKPOINTS_TABLE": checkpoints.table_name,
            "CHECKPOINT_OFFLOAD_BUCKET": checkpoint_offload.bucket_name,
            "LEDGER_TABLE": ledger.table_name,
            "TOMBSTONES_TABLE": tombstones.table_name,
            "IDEMPOTENCY_TABLE": idempotency.table_name,
            "SIGNING_KEY_ARN": signing_key.key_arn,
            # The substitution defence in validate_manifest: a valid signature from a
            # key outside this set does not validate.
            "TRUSTED_SIGNING_KEY_ARNS": signing_key.key_arn,
            "SAGA_DLQ_URL": self.dlq.queue_url,
            "SAGA_DLQ_ARN": self.dlq.queue_arn,
            "RESUME_FUNCTION_ARN": resume_arn,
            "SCHEDULER_ROLE_ARN": self.scheduler_role.role_arn,
        }
        if discovery_runtime_arn:
            environment["DISCOVERY_RUNTIME_ARN"] = discovery_runtime_arn
        if not prod:
            environment["SWEEP_DELAYS_SECONDS"] = _DEV_SWEEP_DELAYS
            environment["APPROVAL_TIMEOUT_SECONDS"] = _DEV_APPROVAL_TIMEOUT
            # `deps.py` has read GRACE_SECONDS_OVERRIDE since M5 and no stack ever set
            # it, so a dev saga fell back to the manifest's graceWindowDays — 30 days.
            # Nothing failed: the integration suite constructs `SagaDeps` directly and
            # passes its own override, so the one path that needed this was the one path
            # that never exercised it. `make walkthrough` is that path, and it would have
            # sat at the grace gate for a month (V11-1).
            environment["GRACE_SECONDS_OVERRIDE"] = _DEV_GRACE_WINDOW

        self.executor_fn = self._saga_function(
            "SagaExecutor",
            stage=stage,
            name="saga-executor",
            handler="pii_erasure.saga.handler.lambda_handler",
            environment=environment,
            description="ASDP saga executor: drives the graph to the next interrupt or END",
        )
        self.resume_fn = self._saga_function(
            "SagaResume",
            stage=stage,
            name="saga-resume",
            handler="pii_erasure.scheduler.handler.lambda_handler",
            environment=environment,
            description="ASDP resume: stale-wake filter + dedup, then Command(resume=...)",
        )

        # ── The saga's ONLY route to the reasoning plane (invariant 12) ─────
        # One action, one ARN, and on the EXECUTOR ALONE. The resume Lambda replays a
        # checkpointed manifest and must never re-plan: a fresh manifest after an
        # approval would execute a plan nobody approved (invariant 3), so withholding
        # this from the resume role makes that mistake un-makeable rather than
        # merely uncoded.
        #
        # `plan` invokes the Runtime and receives JSON; it holds no model client, and
        # neither role holds any `bedrock:*`. That distinction is the invariant: "ask
        # something else to reason and get a plan back" is expressible in IAM;
        # "reason, but only about plans" is not.
        if discovery_runtime_arn and self.executor_fn.role is not None:
            self.executor_fn.role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="PlanNodeInvokesDiscovery",
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[discovery_runtime_arn, f"{discovery_runtime_arn}/*"],
                )
            )

        for fn in (self.executor_fn, self.resume_fn):
            self._grant_data_plane(
                fn,
                checkpoints=checkpoints,
                checkpoint_offload=checkpoint_offload,
                ledger=ledger,
                tombstones=tombstones,
                idempotency=idempotency,
                signing_key=signing_key,
                participants=participants,
                stage=stage,
            )

        for name, value in {
            "SagaExecutorFunctionArn": self.executor_fn.function_arn,
            "SagaResumeFunctionArn": self.resume_fn.function_arn,
            "SagaDlqUrl": self.dlq.queue_url,
            "SchedulerRoleArn": self.scheduler_role.role_arn,
        }.items():
            CfnOutput(self, name, value=value)

    def _saga_function(
        self,
        construct_id: str,
        *,
        stage: str,
        name: str,
        handler: str,
        environment: dict[str, str],
        description: str,
    ) -> lambda_.Function:
        """One saga-plane function. No VPC, ever — asserted at synth time.

        The execution role is named explicitly because M6's Cedar policies match the
        principal on `principal.id like "*:assumed-role/asdp-<stage>-saga-executor"`.
        CDK's generated role names embed a construct hash that changes on replacement,
        which would silently unbind the policy from the identity it exists to
        authorise — a permit that matches nothing, denying everything, at the moment
        of a routine refactor.
        """
        role = iam.Role(
            self,
            f"{construct_id}Role",
            role_name=f"asdp-{stage}-{name}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            # ASCII only: IAM's roleDescriptionType forbids the em dash this repo's
            # prose uses everywhere else. See tests/unit/test_cfn_descriptions.py (V10-2).
            description=f"ASDP {name} execution role. No bedrock:* - invariant 12.",
        )
        return lambda_.Function(
            self,
            construct_id,
            role=role,
            function_name=f"asdp-{stage}-{name}",
            runtime=_RUNTIME,
            handler=handler,
            code=lambda_.Code.from_asset(SAGA_ASSET),
            # The graph runs to its next interrupt inside one invocation; phase 3
            # visits eight participants sequentially, one of which may wait ~2min
            # for an Aurora resume (V8-7). 15 minutes is the documented saga
            # ceiling (ARCHITECTURE §16) — this is that ceiling, not a guess.
            timeout=Duration.minutes(15),
            memory_size=1024,
            environment=environment,
            description=description,
        )

    def _grant_data_plane(
        self,
        fn: lambda_.Function,
        *,
        checkpoints: dynamodb.ITable,
        checkpoint_offload: s3.IBucket,
        ledger: dynamodb.ITable,
        tombstones: dynamodb.ITable,
        idempotency: dynamodb.ITable,
        signing_key: kms.IKey,
        participants: dict[str, lambda_.IFunction],
        stage: str,
    ) -> None:
        checkpoints.grant_read_write_data(fn)
        checkpoint_offload.grant_read_write(fn)
        # DynamoDBSaver configures the offload bucket's TTL lifecycle rule at
        # construction (verified in the installed 1.2.0 source) — these two actions
        # exist for that call and nothing else.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration"],
                resources=[checkpoint_offload.bucket_arn],
            )
        )
        ledger.grant_read_write_data(fn)
        tombstones.grant_read_write_data(fn)
        idempotency.grant_read_write_data(fn)
        signing_key.grant(fn, "kms:Sign", "kms:Verify")
        self.dlq.grant_send_messages(fn)

        # Exactly the eight participants — never a wildcard. A ninth reachable
        # function would be a ninth thing a compromised executor could call.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[p.function_arn for p in participants.values()],
            )
        )
        # One-shot schedules in the default group, own prefix only; PassRole is
        # scoped to the scheduler role AND to the scheduler service, so this
        # permission cannot be repurposed to hand the role to anything else.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["scheduler:CreateSchedule"],
                resources=[
                    self.format_arn(
                        service="scheduler",
                        resource="schedule",
                        resource_name=f"default/asdp-{stage}-*",
                        arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                    )
                ],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[self.scheduler_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
