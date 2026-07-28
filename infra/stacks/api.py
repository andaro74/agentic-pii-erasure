"""The operator API — Cognito-authenticated HTTP API in front of the saga (§8.2).

Two properties carry the whole stack, and both are asserted at synth time rather than
trusted:

**Every route has an authorizer.** One unauthenticated route on this API is the entire
human-in-the-loop control gone — an approval that no human made, recorded in the ledger as
one that they did. The authorizer is set as `default_authorizer` on the API so it applies
to routes rather than being attached per route and forgotten on the next one, and
`test_api_synth.py` reads the synthesised template and fails on any `AWS::ApiGatewayV2::
Route` without an `AuthorizerId`.

**Operators authenticate against a different user pool than data subjects.** The
`cognito-identity` participant owns a pool of *subjects* — the people whose data gets
erased. If those two pools were one, a data subject could obtain a token for the approval
API, and self-approval of one's own erasure is the least of what that would allow. They
are separate constructs in separate stacks, and a synth assertion checks the API's
authorizer does not point at the subjects pool.

The API Lambda holds no graph and no participant permission. Its only grant is
`lambda:InvokeFunction` on the saga executor, so the blast radius of a compromised front
door is "can ask the saga to do things the saga already validates" rather than "can reach
DynamoDB".
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as apigw_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from stacks.saga import SAGA_ASSET

_RUNTIME = lambda_.Runtime.PYTHON_3_12

#: Cognito groups the API checks on the *authorizer's* claims. Named here and in
#: `approval/api.py`; a mismatch would mean nobody can approve anything, which is a
#: loud failure rather than a quiet one — the right direction for this pair to break in.
APPROVER_GROUP = "asdp-approvers"
LEGAL_GROUP = "asdp-legal"

#: The routes, and the whole surface. Each is (method, path) — kept as a constant so the
#: synth test can assert the deployed route set matches it exactly, which is how a route
#: added later without an authorizer gets noticed.
ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/requests"),
    ("GET", "/threads"),
    ("GET", "/threads/{sagaId}"),
    ("POST", "/threads/{sagaId}/approve"),
)


class ApiStack(Stack):
    """The operator front door: an HTTP API, its operator pool, and one Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        saga_executor: lambda_.IFunction,
        prod: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        # ── Operators, NOT subjects ──────────────────────────────────────────
        # A deliberately separate pool from the one `cognito-identity` erases from.
        # Sharing it would let a data subject authenticate to the approval API.
        self.operators = cognito.UserPool(
            self,
            "OperatorPool",
            user_pool_name=f"asdp-{stage}-operators",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_symbols=True,
                require_lowercase=True,
                require_uppercase=True,
            ),
            # MFA is the right default for an identity that authorises irreversible
            # deletion. OPTIONAL rather than REQUIRED only so a dev stack can be driven
            # by the walkthrough; prod pins it on.
            mfa=cognito.Mfa.REQUIRED if prod else cognito.Mfa.OPTIONAL,
            removal_policy=RemovalPolicy.RETAIN if prod else RemovalPolicy.DESTROY,
        )
        for group in (APPROVER_GROUP, LEGAL_GROUP):
            cognito.CfnUserPoolGroup(
                self,
                f"Group{group.title().replace('-', '')}",
                user_pool_id=self.operators.user_pool_id,
                group_name=group,
                description=f"ASDP operators permitted to act as {group}",
            )

        self.client = self.operators.add_client(
            "OperatorClient",
            user_pool_client_name=f"asdp-{stage}-operator-client",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            # No implicit flow, no client secret: the CLI is a public client using
            # USER_PASSWORD_AUTH, and a secret in a CLI is a secret in a shell history.
            generate_secret=False,
            access_token_validity=Duration.hours(1),
        )

        # ── The handler ──────────────────────────────────────────────────────
        role = iam.Role(
            self,
            "ApprovalApiRole",
            role_name=f"asdp-{stage}-approval-service",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
            # ASCII only - IAM's roleDescriptionType rejects em dashes (V10-2).
            description="ASDP approval service. Invokes the saga executor and nothing else.",
        )
        self.handler = lambda_.Function(
            self,
            "ApprovalApi",
            role=role,
            function_name=f"asdp-{stage}-approval-api",
            runtime=_RUNTIME,
            handler="pii_erasure.approval.api.lambda_handler",
            code=lambda_.Code.from_asset(SAGA_ASSET),
            # This comment used to end "…so this timeout exists to fail before the
            # gateway does rather than after", and every clause of it was true: the saga
            # may run 15 minutes, API Gateway allows 30 seconds. The conclusion drawn was
            # "pick a timeout that fails cleanly". The available conclusion was **this
            # call cannot be synchronous** — and intake now is not (V11-3).
            #
            # 29 seconds still bounds what remains: reads and the approval resume, both
            # of which are a checkpoint read plus at most a schedule write. If either
            # ever approaches this, that is the signal to make it asynchronous too, not
            # to raise the number.
            timeout=Duration.seconds(29),
            memory_size=512,
            environment={
                "PII_ERASURE_STAGE": stage,
                "SAGA_EXECUTOR_FUNCTION": saga_executor.function_name,
            },
            description="ASDP operator API: intake, approval, and operator reads",
        )
        # The only grant. No DynamoDB, no KMS, no participant: a compromised front door
        # can ask the saga for things the saga already validates, and nothing more.
        saga_executor.grant_invoke(self.handler)

        # ── The API ──────────────────────────────────────────────────────────
        authorizer = apigw_authorizers.HttpUserPoolAuthorizer(
            "OperatorAuthorizer",
            self.operators,
            user_pool_clients=[self.client],
            authorizer_name=f"asdp-{stage}-operators",
        )
        self.api = apigw.HttpApi(
            self,
            "OperatorApi",
            api_name=f"asdp-{stage}-operator-api",
            description="ASDP operator API. Every route is Cognito-authenticated.",
            # Set as the DEFAULT rather than per route. A route added later inherits it;
            # a per-route attachment is one that the next route forgets.
            default_authorizer=authorizer,
        )
        integration = apigw_integrations.HttpLambdaIntegration("ApprovalIntegration", self.handler)
        for method, path in ROUTES:
            self.api.add_routes(
                path=path,
                methods=[apigw.HttpMethod(method)],
                integration=integration,
            )

        for name, value in {
            "OperatorApiUrl": self.api.url or "",
            "OperatorPoolId": self.operators.user_pool_id,
            "OperatorClientId": self.client.user_pool_client_id,
            "ApprovalApiFunctionArn": self.handler.function_arn,
        }.items():
            CfnOutput(self, name, value=value)
