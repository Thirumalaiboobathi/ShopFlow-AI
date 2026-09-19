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
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

PREFIX = "shopflow"


class ShopFlowStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 *, alert_email: str | None = None, monthly_budget_usd: int = 25,
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
            handler="handler.handler",
            code=lambda_.Code.from_asset("../backend/lambdas/health"),
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

        # Throttle the default stage. A public endpoint with no ceiling is an
        # open invitation, and the ship gate depends on this staying up.
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
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.minutes(5)),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200,
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
