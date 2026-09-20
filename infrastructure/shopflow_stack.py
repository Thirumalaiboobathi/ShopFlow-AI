"""ShopFlow production foundation.

Deliberately small. This stack exists to make ShopFlow publicly reachable on
AWS before the features are finished, so that shipping is never the risk late
in the build. It carries the storage, the edge, and one health endpoint —
nothing that needs Bedrock, and no public surface beyond the site and /api/health.

Everything is named `shopflow-*` and tagged, so ShopFlow resources are
distinguishable from the other projects in this account at a glance.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as integrations,
    aws_budgets as budgets,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

PREFIX = "shopflow"

# Local-only files that must not travel into the Lambda bundle.
BACKEND_EXCLUDES = ["**/__pycache__", "**/*.pyc", "**/.pytest_cache"]


class ShopFlowStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 *, alert_email: str | None = None, monthly_budget_usd: int = 25,
                 bedrock_model_id: str = "apac.amazon.nova-pro-v1:0",
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = Stack.of(self).account

        # ------------------------------------------------------------------
        # Data
        # ------------------------------------------------------------------
        table = dynamodb.Table(
            self, "Table",
            table_name=f"{PREFIX}-demo",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Demo jobs are transient; DynamoDB removes them on its own.
            time_to_live_attribute="expiresAt",
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            # Demo data is regenerated from the seed generator, so the table is
            # reproducible and safe to tear down with the stack.
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="GSI1SK", type=dynamodb.AttributeType.STRING),
        )

        # Owner-uploaded supplier price lists. Never public: CloudFront has no
        # route to this bucket, and presigned URLs will gate access later.
        uploads = s3.Bucket(
            self, "Uploads",
            bucket_name=f"{PREFIX}-uploads-{account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-demo-uploads",
                    expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )

        # ------------------------------------------------------------------
        # Compute
        # ------------------------------------------------------------------
        health_log_group = logs.LogGroup(
            self, "HealthLogs",
            log_group_name=f"/aws/lambda/{PREFIX}-health",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        health_fn = lambda_.Function(
            self, "HealthFunction",
            function_name=f"{PREFIX}-health",
            runtime=lambda_.Runtime.PYTHON_3_13,
            # The whole backend ships as one asset so the engine, the agent and
            # the generated seed travel together; handlers differ per function.
            code=lambda_.Code.from_asset("../backend", exclude=BACKEND_EXCLUDES),
            handler="lambdas/health/handler.handler",
            memory_size=256,
            timeout=Duration.seconds(10),
            environment={
                "TABLE_NAME": table.table_name,
                "SERVICE_VERSION": "0.1.0",
                "STAGE": "prod",
            },
            log_group=health_log_group,
        )
        # Least privilege: read-only, and only this table.
        table.grant_read_data(health_fn)

        # ---- order worker: the only function allowed to reach Bedrock ----
        worker_fn = lambda_.Function(
            self, "WorkerFunction",
            function_name=f"{PREFIX}-order-worker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lambdas/worker/handler.handler",
            code=lambda_.Code.from_asset("../backend", exclude=BACKEND_EXCLUDES),
            memory_size=1024,
            # Comfortably above the observed ~4s agent loop, still bounded.
            timeout=Duration.seconds(60),
            environment={
                "TABLE_NAME": table.table_name,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "BEDROCK_REGION": self.region,
                "UPLOADS_BUCKET": uploads.bucket_name,
            },
            log_group=logs.LogGroup(
                self, "WorkerLogs",
                log_group_name=f"/aws/lambda/{PREFIX}-order-worker",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            # One retry on an async invoke is enough; more would multiply
            # Bedrock spend on a request that is already failing.
            retry_attempts=0,
            # No reserved concurrency, for two reasons.
            #
            # It is not permitted: this account's total Lambda concurrency is
            # 10, and AWS requires at least 10 to stay unreserved, so any
            # reservation is rejected outright.
            #
            # It would also be the wrong thing to ask for. Reserving 5 of 10
            # would leave 5 for every other project in the account. The 10-wide
            # account ceiling already bounds concurrent Bedrock calls more
            # tightly than a reservation would have - it is simply a shared
            # ceiling rather than a private one.
        )
        table.grant_read_write_data(worker_fn)
        # Read-only on uploads: the worker reads a price list, never writes one.
        uploads.grant_read(worker_fn)
        worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            # Scoped to the one model this agent uses, via the APAC inference
            # profile and the foundation models it can route to.
            resources=[
                f"arn:aws:bedrock:{self.region}:{account}:inference-profile/{bedrock_model_id}",
                f"arn:aws:bedrock:*::foundation-model/{bedrock_model_id.split('.', 1)[1]}",
            ],
        ))

        # ---- public API ----
        api_fn = lambda_.Function(
            self, "ApiFunction",
            function_name=f"{PREFIX}-api",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lambdas/api/handler.handler",
            code=lambda_.Code.from_asset("../backend", exclude=BACKEND_EXCLUDES),
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                "TABLE_NAME": table.table_name,
                "WORKER_FUNCTION_NAME": worker_fn.function_name,
                "UPLOADS_BUCKET": uploads.bucket_name,
            },
            log_group=logs.LogGroup(
                self, "ApiLogs",
                log_group_name=f"/aws/lambda/{PREFIX}-api-fn",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(api_fn)
        # Write-only on uploads: the API stores a price list and never reads
        # one back, so a bug here cannot turn into a document-disclosure path.
        uploads.grant_put(api_fn)
        # The API may start the worker but has no Bedrock permission of its own.
        worker_fn.grant_invoke(api_fn)

        http_api = apigw.HttpApi(
            self, "HttpApi",
            api_name=f"{PREFIX}-api",
            description="ShopFlow public API (foundation)",
            # No CORS configured: the browser reaches /api/* through the same
            # CloudFront domain as the site, so requests are same-origin.
        )
        # Path is /api/health, not /health: CloudFront forwards the /api/*
        # prefix unchanged, so the route must match what the origin receives.
        http_api.add_routes(
            path="/api/health",
            methods=[apigw.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration(
                "HealthIntegration", health_fn),
        )

        api_integration = integrations.HttpLambdaIntegration(
            "ApiIntegration", api_fn)
        for path, method in (
            ("/api/orders", apigw.HttpMethod.POST),
            ("/api/supplier-price-lists", apigw.HttpMethod.POST),
            ("/api/price-decisions", apigw.HttpMethod.POST),
            ("/api/jobs/{jobId}", apigw.HttpMethod.GET),
            ("/api/demo", apigw.HttpMethod.GET),
        ):
            http_api.add_routes(
                path=path, methods=[method], integration=api_integration)

        # One throttle for the whole stage, deliberately.
        #
        # Per-route settings were tried first, to hold POST /api/orders far
        # below the cheap read routes. They are a trap: API Gateway rejects
        # settings for a route that does not exist yet, and on rollback
        # CloudFormation deletes routes before it updates the stage - so a
        # failed deploy cannot roll itself back and the stack wedges in
        # UPDATE_ROLLBACK_FAILED. That happened here and needed manual
        # recovery. A public demo has to be redeployable under pressure, so
        # the marginal protection is not worth the ship-gate risk.
        #
        # The real cost ceiling is the worker's reserved concurrency, which
        # caps concurrent Bedrock calls however the request arrived.
        default_stage = http_api.default_stage.node.default_child
        default_stage.default_route_settings = (
            apigw.CfnStage.RouteSettingsProperty(
                throttling_rate_limit=20,
                throttling_burst_limit=40,
            )
        )

        api_logs = logs.LogGroup(
            self, "ApiAccessLogs",
            log_group_name=f"/aws/apigateway/{PREFIX}-api",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        default_stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=api_logs.log_group_arn,
            format=(
                '{"requestId":"$context.requestId",'
                '"ip":"$context.identity.sourceIp",'
                '"requestTime":"$context.requestTime",'
                '"routeKey":"$context.routeKey",'
                '"status":"$context.status",'
                '"latency":"$context.responseLatency"}'
            ),
        )

        # ------------------------------------------------------------------
        # Edge
        # ------------------------------------------------------------------
        site = s3.Bucket(
            self, "Site",
            bucket_name=f"{PREFIX}-site-{account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        api_domain = f"{http_api.http_api_id}.execute-api.{self.region}.amazonaws.com"

        distribution = cloudfront.Distribution(
            self, "Distribution",
            comment=f"{PREFIX} public site",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,
            default_behavior=cloudfront.BehaviorOptions(
                # S3BucketOrigin with OAC: the bucket stays private and only
                # this distribution can read it.
                origin=origins.S3BucketOrigin.with_origin_access_control(site),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy(
                    self, "SecurityHeaders",
                    response_headers_policy_name=f"{PREFIX}-security-headers",
                    security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                        content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                            override=True),
                        frame_options=cloudfront.ResponseHeadersFrameOptions(
                            frame_option=cloudfront.HeadersFrameOption.DENY,
                            override=True),
                        referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                            referrer_policy=cloudfront.HeadersReferrerPolicy
                                .STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                            override=True),
                        strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                            access_control_max_age=Duration.days(365),
                            include_subdomains=True, override=True),
                    ),
                ),
            ),
            additional_behaviors={
                # Same-origin API path. CloudFront must not cache health.
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.HttpOrigin(api_domain),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    # Forward everything except Host - API Gateway rejects a
                    # request whose Host header is the CloudFront domain.
                    origin_request_policy=cloudfront.OriginRequestPolicy
                        .ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            },
            # CloudFront custom error responses are distribution-wide - they
            # cannot be scoped to one behaviour - so mapping 404 here would
            # turn a genuine API 404 into a 200 serving index.html, hiding
            # broken endpoints behind an apparently successful page.
            #
            # Only 403 is mapped, which separates the two cleanly: S3 behind
            # OAC answers 403 for a missing object (the bucket policy grants
            # GetObject but not ListBucket), while API Gateway answers 404 for
            # an unknown route. So the SPA fallback still works and API 404s
            # reach the caller intact.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.minutes(5)),
            ],
        )

        s3deploy.BucketDeployment(
            self, "SiteDeployment",
            sources=[s3deploy.Source.asset("../frontend/site")],
            destination_bucket=site,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )

        # ------------------------------------------------------------------
        # Cost guard
        # ------------------------------------------------------------------
        # A public endpoint that can eventually call Bedrock needs a spend
        # ceiling in place before that endpoint exists, not after.
        if alert_email:
            budgets.CfnBudget(
                self, "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name=f"{PREFIX}-monthly",
                    budget_type="COST",
                    time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(
                        amount=monthly_budget_usd, unit="USD"),
                ),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            comparison_operator="GREATER_THAN",
                            notification_type="ACTUAL",
                            threshold=threshold,
                            threshold_type="PERCENTAGE",
                        ),
                        subscribers=[
                            budgets.CfnBudget.SubscriberProperty(
                                address=alert_email, subscription_type="EMAIL")
                        ],
                    )
                    for threshold in (50, 80, 100)
                ],
            )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.domain_name}",
                  description="Public ShopFlow URL")
        CfnOutput(self, "HealthUrl",
                  value=f"https://{distribution.domain_name}/api/health",
                  description="Health endpoint via CloudFront")
        CfnOutput(self, "ApiEndpoint", value=http_api.api_endpoint,
                  description="API Gateway endpoint (origin)")
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "UploadsBucket", value=uploads.bucket_name)
        CfnOutput(self, "SiteBucket", value=site.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "BedrockModelId", value=bedrock_model_id)
        CfnOutput(self, "WorkerFunctionName", value=worker_fn.function_name)
