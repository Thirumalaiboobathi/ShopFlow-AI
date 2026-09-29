"""ShopFlow production foundation.

Deliberately small. This stack exists to make ShopFlow publicly reachable on
AWS before the features are finished, so that shipping is never the risk late
in the build. It carries the storage, the edge, and one health endpoint —
nothing that needs Bedrock, and no public surface beyond the site and /api/health.

Everything is named `shopflow-*` and tagged, so ShopFlow resources are
distinguishable from the other projects in this account at a glance.
"""

from __future__ import annotations

import pathlib

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_integrations as integrations,
    aws_budgets as budgets,
    aws_cloudfront as cloudfront,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_events,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sqs as sqs,
)
from constructs import Construct

PREFIX = "shopflow"

# Local-only files that must not travel into the Lambda bundle.
BACKEND_EXCLUDES = ["**/__pycache__", "**/*.pyc", "**/.pytest_cache"]

# Asset paths, resolved from this file rather than from the working directory.
#
# `Code.from_asset("../backend")` only worked when cdk was run from
# infrastructure/, which is how the deploy script runs it - but it made the
# stack impossible to synthesize from anywhere else, including from a test.
# The content is identical either way, and an asset hash is computed from the
# content, so this changes where the files are found and nothing else.
_HERE = pathlib.Path(__file__).resolve().parent
BACKEND_ASSET = str(_HERE.parent / "backend")
SITE_ASSET = str(_HERE.parent / "frontend" / "site")


class ShopFlowStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 *, alert_email: str | None = None, monthly_budget_usd: int = 25,
                 bedrock_model_id: str = "apac.amazon.nova-pro-v1:0",
                 # WhatsApp Business Cloud API. Every one of these is optional
                 # and the feature is OFF unless `whatsapp_enabled` is set, so
                 # a deploy with no WhatsApp context adds no environment
                 # variable of consequence and no IAM statement at all.
                 whatsapp_enabled: bool = False,
                 whatsapp_phone_number_id: str | None = None,
                 whatsapp_token_secret_arn: str | None = None,
                 whatsapp_template_name: str | None = None,
                 whatsapp_api_version: str | None = None,
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
                ),
                # Voice recordings are transient. The API deletes each object
                # as soon as its transcript is read or its job fails; this rule
                # is the backstop for the case where that cleanup did not run.
                # One day, not thirty: a recording of a customer's voice is not
                # something to keep because nobody got round to deleting it.
                s3.LifecycleRule(
                    id="expire-voice-audio",
                    prefix="voice-audio/",
                    expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
            ],
        )

        # ------------------------------------------------------------------
        # Business events and owner alerts
        # ------------------------------------------------------------------
        # A custom bus rather than the default one. The default bus carries
        # every AWS service event in the account, so a rule on it has to
        # filter against traffic this application does not produce; a bus of
        # its own means a ShopFlow rule matches ShopFlow events and nothing
        # else, and it costs nothing - EventBridge bills per million events
        # published, not per bus.
        event_bus = events.EventBus(
            self, "BusinessEvents",
            event_bus_name=f"{PREFIX}-business-events",
        )

        # Where owner alerts go. The topic exists whether or not anybody is
        # subscribed to it, because the rules below need a target and because
        # a topic with no subscription is the correct configuration for a demo
        # nobody wants email from.
        alerts_topic = sns.Topic(
            self, "OwnerAlerts",
            topic_name=f"{PREFIX}-owner-alerts",
            display_name="ShopFlow owner alerts",
        )

        # Operational alarms have a topic of their own.
        #
        # They used to publish to the owner-alerts topic. With nobody
        # subscribed that changed nothing, but the day the owner subscribed
        # for price alerts they would also have been sent "worker errors
        # sustained" and "orders queue backlog" - infrastructure alarms mixed
        # into business notifications. The owner topic now receives exactly
        # the two business events its rule routes, and nothing else.
        #
        # No subscription is made here either: whoever operates the stack
        # subscribes to this one deliberately.
        ops_topic = sns.Topic(
            self, "OpsAlarms",
            topic_name=f"{PREFIX}-ops-alarms",
            display_name="ShopFlow operational alarms",
        )

        # A topic policy is written for CloudWatch explicitly, scoped to this
        # account's own ShopFlow alarms. (On the owner topic the EventBridge
        # target's policy replaced the default one and admitted
        # events.amazonaws.com only; an alarm action there needed this
        # statement to publish at all. It now lives on the topic the alarms
        # actually use.)
        ops_topic.add_to_resource_policy(iam.PolicyStatement(
            sid="AllowShopFlowAlarms",
            principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
            actions=["sns:Publish"],
            resources=[ops_topic.topic_arn],
            conditions={
                "StringEquals": {"aws:SourceAccount": self.account},
                "ArnLike": {"aws:SourceArn":
                            f"arn:aws:cloudwatch:{self.region}:{self.account}"
                            f":alarm:{PREFIX}-*"},
            },
        ))

        # Subscribing is opt-in and off by default.
        #
        # `alertEmail` is already required for the cost budget, so reusing it
        # would silently sign the deployer up for business alerts the moment
        # this stack shipped - an email address given for one purpose being
        # used for another. It takes a second, deliberate flag.
        #
        # No address is written down here. If the flag is set, the address
        # comes from the same context the budget uses; if it is not, the topic
        # stays empty and every alert below is published and dropped, which
        # changes nothing about any order, quotation or plan.
        business_alerts_enabled = str(
            self.node.try_get_context("enableBusinessAlerts") or ""
        ).lower() in ("1", "true", "yes")
        if business_alerts_enabled and alert_email:
            alerts_topic.add_subscription(
                sns_subs.EmailSubscription(alert_email))
        # Told to both Lambdas so the page can say, truthfully, whether an
        # alert reaches a person. EMAIL only when a subscription was made here.
        alert_delivery = ("EMAIL" if business_alerts_enabled and alert_email
                          else "NONE")

        # Which events are worth interrupting somebody for: two.
        #
        # A supplier price alert (de-duplicated: one per price move) and the
        # once-a-day brief. Everything else is still published to the bus and
        # counted, but not routed to a person. A live evaluation measured 216
        # routed events in three hours of testing - a StockoutDetected for
        # every quotation with a short line, a LowMarginDetected per order, a
        # PurchasePlanGenerated per plan request - none of them de-duplicated.
        # An owner emailed that often learns to ignore the emails, including
        # the one that mattered. The brief already carries today's shortages,
        # margin risks and the plan, once.
        alerting_events = [
            "SupplierPriceChanged",
            "DailyShopBriefGenerated",
        ]
        events.Rule(
            self, "OwnerAlertRule",
            rule_name=f"{PREFIX}-owner-alerts",
            description=(
                "Routes supplier price alerts and the daily shop brief to the "
                "owner-alerts SNS topic. Per-order and per-plan events stay "
                "on the bus: the brief summarises them once a day."),
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=["shopflow.business"],
                detail_type=alerting_events,
            ),
            targets=[events_targets.SnsTopic(alerts_topic)],
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
            code=lambda_.Code.from_asset(BACKEND_ASSET, exclude=BACKEND_EXCLUDES),
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

        # ---- the order queue ---------------------------------------------
        #
        # This replaced a direct `lambda.invoke(InvocationType="Event")`.
        # That call had no durability: if the invoke failed, the job row sat
        # at QUEUED until its TTL expired, the browser polled something that
        # would never finish, and nothing recorded that an order had been
        # lost. A queue makes the work durable, bounds the retries, and gives
        # a dead-letter queue that a person can actually look inside.
        #
        # Standard, not FIFO. Nothing here needs ordering - each job is
        # independent and keyed by its own id - and FIFO's exactly-once
        # processing would be solving a problem the worker already solves
        # with a conditional write. FIFO would also cap throughput per message
        # group for no benefit.
        orders_dlq = sqs.Queue(
            self, "OrdersDeadLetterQueue",
            queue_name=f"{PREFIX}-orders-dlq",
            # 14 days, the maximum. A dead-lettered job is evidence of a
            # defect and the point of it is that somebody gets to read it;
            # the default four days can easily span a weekend and a holiday.
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        orders_queue = sqs.Queue(
            self, "OrdersQueue",
            queue_name=f"{PREFIX}-orders",
            # Six times the worker's 60s timeout, which is the figure AWS
            # documents for a Lambda consumer. It is derived from that
            # timeout rather than picked: at 60s a message could come back
            # into view while the first attempt was still running, and the
            # job would be processed twice concurrently. The worker would
            # survive that - `_claim` is a conditional write - but paying
            # twice for Bedrock to reach the same answer is not a thing to
            # design in on purpose.
            visibility_timeout=Duration.seconds(360),
            # Four days. Longer than the job records themselves, which carry a
            # 24-hour TTL, so the queue can never be the reason a job is lost.
            retention_period=Duration.days(4),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            dead_letter_queue=sqs.DeadLetterQueue(
                # Three attempts: the first, and two retries.
                #
                # Chosen from what actually fails here. The retryable failures
                # on this path are Bedrock throttles and transient 5xx, and
                # those clear in seconds. Three attempts spread over roughly
                # twelve minutes of visibility timeout is more than enough
                # room for that.
                #
                # Higher would be worse, not safer: every retry is another
                # full Bedrock tool loop, so five attempts on a genuinely
                # broken request is five times the spend to reach the same
                # failure. Three is the point where more attempts stop buying
                # recovery and start buying cost.
                max_receive_count=3,
                queue=orders_dlq,
            ),
        )

        # WhatsApp is off unless switched on deliberately. With no context
        # supplied the only variable added reads "false": the API falls back
        # to the wa.me draft and the inbound webhook answers 404.
        whatsapp_env = {
            "WHATSAPP_API_ENABLED": "true" if whatsapp_enabled else "false",
            **({"WHATSAPP_PHONE_NUMBER_ID": whatsapp_phone_number_id}
               if whatsapp_phone_number_id else {}),
            # The ARN of a secret, never the token itself. No credential
            # value is ever placed in a Lambda environment by this stack. The
            # secret holds the access token, the app secret (the webhook's
            # signature key) and the webhook verify token.
            **({"WHATSAPP_TOKEN_SECRET_ARN": whatsapp_token_secret_arn}
               if whatsapp_token_secret_arn else {}),
            **({"WHATSAPP_TEMPLATE_NAME": whatsapp_template_name}
               if whatsapp_template_name else {}),
            **({"WHATSAPP_API_VERSION": whatsapp_api_version}
               if whatsapp_api_version else {}),
        }

        # ---- order worker: the only function allowed to reach Bedrock ----
        worker_fn = lambda_.Function(
            self, "WorkerFunction",
            function_name=f"{PREFIX}-order-worker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lambdas/worker/handler.handler",
            code=lambda_.Code.from_asset(BACKEND_ASSET, exclude=BACKEND_EXCLUDES),
            memory_size=1024,
            # Comfortably above the observed ~4s agent loop, still bounded.
            timeout=Duration.seconds(60),
            environment={
                "TABLE_NAME": table.table_name,
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "BEDROCK_REGION": self.region,
                "UPLOADS_BUCKET": uploads.bucket_name,
                # Empty would switch business events off; the worker publishes
                # to this bus and carries on regardless if it cannot.
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "ALERT_DELIVERY": alert_delivery,
                # WhatsApp replies to inbound customer messages are sent from
                # here, after the order is read. The same switch and the same
                # secret ARN as the API; never a credential value.
                **whatsapp_env,
            },
            log_group=logs.LogGroup(
                self, "WorkerLogs",
                log_group_name=f"/aws/lambda/{PREFIX}-order-worker",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            # No async invoke configuration any more. SQS drives this
            # function synchronously through an event source mapping, so
            # retries come from the queue's redrive policy above - three
            # attempts, then the dead-letter queue - and `retry_attempts`,
            # which only applies to asynchronous invocation, would have no
            # effect if it were still set here.
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
        # ---- the worker consumes the queue -------------------------------
        #
        # This mapping is also the IAM grant: CDK gives the worker's role
        # ReceiveMessage, DeleteMessage, GetQueueAttributes and
        # ChangeMessageVisibility on THIS queue only. No sqs:* and no
        # wildcard resource.
        worker_fn.add_event_source(lambda_events.SqsEventSource(
            orders_queue,
            # One message per invocation.
            #
            # A batch would risk the whole batch's visibility timeout against
            # the slowest order in it, and a failure part-way through a batch
            # re-runs the messages that already succeeded. With a batch of
            # one, raising an exception means exactly one job is retried, and
            # the meaning of a retry stays simple enough to reason about.
            batch_size=1,
            # THE IMPORTANT LINE.
            #
            # This account's total Lambda concurrency is 10, shared with every
            # other project in it. Left unbounded, a burst of queued orders
            # would scale this worker out until there was no concurrency left
            # for the API function - and the public site would start failing
            # while the queue drained.
            #
            # Two, not five, and the reason is Bedrock, not Lambda. The
            # account's quota for Nova Pro is 25 cross-region requests per
            # minute, and one order is two to four Converse calls in about six
            # seconds - one busy worker alone asks for roughly thirty a minute.
            # Throughput is capped by that quota whatever this number is; above
            # two, extra workers only turn queued orders into throttled ones,
            # each of which burns one of its three attempts. An evaluator's
            # burst of ~30 orders at five-wide left five jobs throttled and
            # waiting over six minutes. Two is also the lowest value SQS
            # accepts here. A waiting message costs nothing; a throttled one
            # costs an attempt.
            #
            # Note this is the event source's own limit, not reserved
            # concurrency: reserving is rejected outright on an account with a
            # ceiling of 10, and would also take capacity away from other
            # projects rather than just capping this one.
            max_concurrency=2,
        ))

        table.grant_read_write_data(worker_fn)
        # Read-only on uploads: the worker reads a price list, never writes one.
        uploads.grant_read(worker_fn)
        # Publish only, and only to this bus. The worker never reads an event,
        # creates a rule or describes the bus.
        event_bus.grant_put_events_to(worker_fn)
        # One Textract action, for the one call the document reader makes.
        # `AnalyzeDocument` is synchronous and takes the image in the request,
        # so no Textract-side S3 access is granted and none is needed.
        #
        # Textract has no resource-level ARNs for this action, so the resource
        # is "*". That is the API's own shape rather than a widening: the
        # action itself is the boundary, and it can read only a document this
        # function hands it.
        worker_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["textract:AnalyzeDocument"],
            resources=["*"],
        ))
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
        # ---- the daily shop brief: one schedule, the existing worker ----
        #
        # One rule on the default bus (a schedule cannot live on a custom
        # bus), one target, a constant input the worker recognises. No new
        # function: the worker already has the table, the bus and the one
        # Bedrock permission the optional summary needs. Once a day it costs
        # one invocation and at most one Converse call. The time is context
        # so it can move without a code change; 02:30 UTC is 08:00 in India.
        brief_schedule = (self.node.try_get_context("dailyBriefSchedule")
                          or "cron(30 2 * * ? *)")
        events.Rule(
            self, "DailyBriefSchedule",
            rule_name=f"{PREFIX}-daily-brief",
            description=("Builds the ShopFlow daily shop brief once a day on "
                         "the order worker."),
            schedule=events.Schedule.expression(brief_schedule),
            targets=[events_targets.LambdaFunction(
                worker_fn,
                event=events.RuleTargetInput.from_object(
                    {"shopflowTask": "DAILY_BRIEF"}),
                retry_attempts=1,
            )],
        )

        api_fn = lambda_.Function(
            self, "ApiFunction",
            function_name=f"{PREFIX}-api",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lambdas/api/handler.handler",
            code=lambda_.Code.from_asset(BACKEND_ASSET, exclude=BACKEND_EXCLUDES),
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                "TABLE_NAME": table.table_name,
                "ORDERS_QUEUE_URL": orders_queue.queue_url,
                "UPLOADS_BUCKET": uploads.bucket_name,
                # The API publishes one event: PurchasePlanGenerated, after
                # the plan is already built and returned correctly.
                "EVENT_BUS_NAME": event_bus.event_bus_name,
                "ALERT_DELIVERY": alert_delivery,
                # WhatsApp is off unless switched on deliberately. With no
                # context supplied this is the only variable added, it reads
                # "false", and the API falls back to the wa.me draft.
                **whatsapp_env,
            },
            log_group=logs.LogGroup(
                self, "ApiLogs",
                log_group_name=f"/aws/lambda/{PREFIX}-api-fn",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        table.grant_read_write_data(api_fn)
        # Publish only, to this bus only.
        event_bus.grant_put_events_to(api_fn)
        # Write-only on uploads: the API stores a price list and never reads
        # one back, so a bug here cannot turn into a document-disclosure path.
        uploads.grant_put(api_fn)
        # Send only, to one queue. `grant_send_messages` produces
        # sqs:SendMessage, sqs:GetQueueAttributes and sqs:GetQueueUrl on this
        # queue's ARN - the API cannot receive, cannot delete, cannot purge,
        # and cannot reach the dead-letter queue at all.
        #
        # The previous `worker_fn.grant_invoke(api_fn)` is GONE. The API can
        # no longer invoke the worker by any route, which is what makes the
        # queue the only path into it rather than merely the preferred one.
        orders_queue.grant_send_messages(api_fn)

        # ---- Amazon Transcribe -------------------------------------------
        #
        # Three actions, and only on jobs this application named. The name
        # prefix is what makes the scope possible: `engine.speech.job_name`
        # prefixes every job "shopflow-", so a policy on
        # transcription-job/shopflow-* cannot touch another project's job in
        # this account. No transcribe:* anywhere.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "transcribe:StartTranscriptionJob",
                "transcribe:GetTranscriptionJob",
                "transcribe:DeleteTranscriptionJob",
            ],
            resources=[
                f"arn:aws:transcribe:{self.region}:{account}:"
                f"transcription-job/{PREFIX}-*",
            ],
        ))
        # Transcribe reads the recording using the caller's permissions, and
        # the API deletes it afterwards. Both are scoped to the audio prefix
        # ONLY - the API still cannot read a supplier price list, so the
        # document-disclosure property that grant_put was chosen for survives.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:DeleteObject"],
            resources=[uploads.arn_for_objects("voice-audio/*")],
        ))

        # ---- WhatsApp credentials ----------------------------------------
        #
        # Conditional, and deliberately so: with no secret ARN configured this
        # block adds NOTHING, so the default deployment carries no
        # secretsmanager permission at all. When an ARN is given, the grant is
        # to that one secret - never secretsmanager:* and never a wildcard
        # resource.
        if whatsapp_token_secret_arn:
            # The API reads it for the webhook's signature check; the worker
            # for the access token it replies with. Both to this one secret.
            for fn in (api_fn, worker_fn):
                fn.add_to_role_policy(iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[whatsapp_token_secret_arn],
                ))

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
            ("/api/purchase-plans", apigw.HttpMethod.POST),
            ("/api/shop-queries", apigw.HttpMethod.POST),
            ("/api/customers", apigw.HttpMethod.GET),
            ("/api/customers/{customerId}", apigw.HttpMethod.GET),
            ("/api/credit/check", apigw.HttpMethod.POST),
            ("/api/voice/transcribe", apigw.HttpMethod.POST),
            ("/api/whatsapp/send", apigw.HttpMethod.POST),
            # Meta's webhook: the subscription handshake and the deliveries.
            # Configure Meta with the API Gateway URL, not CloudFront's - the
            # distribution maps 403 to the site's index page.
            ("/api/whatsapp/webhook", apigw.HttpMethod.GET),
            ("/api/whatsapp/webhook", apigw.HttpMethod.POST),
            ("/api/languages", apigw.HttpMethod.GET),
            ("/api/jobs/{jobId}", apigw.HttpMethod.GET),
            ("/api/demo", apigw.HttpMethod.GET),
            ("/api/intelligence", apigw.HttpMethod.GET),
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
            sources=[s3deploy.Source.asset(SITE_ASSET)],
            destination_bucket=site,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=True,
        )

        # ------------------------------------------------------------------
        # Alarms
        # ------------------------------------------------------------------
        # Three, and only three. Each answers a question somebody would
        # actually ask, and each fires on a metric AWS already publishes, so
        # none of them costs a custom metric.
        #
        # Every alarm publishes to the owner-alerts topic when it fires. That
        # topic has NO subscriber unless the deployer opts in with
        # `enableBusinessAlerts` (see above), so on a default deployment the
        # alarm's notification is published to SNS and delivered to nobody.
        # What this changes is that the wiring exists and is exercised: a
        # subscription added in the console or by the flag receives alarms
        # with no redeploy, instead of there being nothing to subscribe to.

        # 1. Anything in the dead-letter queue.
        #
        # Threshold 0, because the correct number of dead-lettered orders is
        # zero. This is not a rate to be tuned - a single message here means a
        # customer's order failed three times and no automatic process will
        # pick it up. One datapoint is enough; waiting for a second would mean
        # waiting for a second lost order.
        ordersDlqNotEmpty_alarm = cloudwatch.Alarm(
            self, "OrdersDlqNotEmpty",
            alarm_name=f"{PREFIX}-orders-dlq-not-empty",
            alarm_description=(
                "An order reached the dead-letter queue after three failed "
                "attempts. The job is still in DynamoDB at PROCESSING and "
                "nothing will retry it automatically. The message body "
                "carries the jobId."),
            metric=orders_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5), statistic="Maximum"),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # 2. The worker is failing repeatedly.
        #
        # Three errors in five minutes, over two consecutive periods. Not one
        # error: a single retryable failure is the system working as designed,
        # because the whole point of the queue is that one failure is
        # survivable. Two periods filters the momentary Bedrock throttle that
        # resolves itself. What this catches is a sustained fault - a bad
        # deploy, a revoked permission, a model that has stopped answering.
        workerErrorsSustained_alarm = cloudwatch.Alarm(
            self, "WorkerErrorsSustained",
            alarm_name=f"{PREFIX}-worker-errors-sustained",
            alarm_description=(
                "The order worker has failed repeatedly for ten minutes. "
                "Individual retryable failures are expected and do not alarm; "
                "this indicates a sustained fault."),
            metric=worker_fn.metric_errors(
                period=Duration.minutes(5), statistic="Sum"),
            threshold=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # 3. Work is sitting in the queue too long.
        #
        # 300 seconds of age. Derived from the workload, not picked: an order
        # is a bounded 6-turn Bedrock loop measured at around four seconds,
        # and the event source runs five of them at a time. A message that has
        # been waiting five minutes is not waiting for a busy worker - it is
        # waiting for one that is stuck, throttled or unable to start.
        #
        # This is the alarm that would have caught the original defect, if the
        # original defect had been capable of leaving a message anywhere.
        ordersQueueBacklog_alarm = cloudwatch.Alarm(
            self, "OrdersQueueBacklog",
            alarm_name=f"{PREFIX}-orders-queue-backlog",
            alarm_description=(
                "The oldest order on the queue has been waiting over five "
                "minutes. An order normally takes seconds, so this means the "
                "worker is not consuming."),
            metric=orders_queue.metric_approximate_age_of_oldest_message(
                period=Duration.minutes(5), statistic="Maximum"),
            threshold=300,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # 4. An order failed inside the agent.
        #
        # This is the gap the first three alarms left. When the agent itself
        # fails, the worker marks the job FAILED and acknowledges the message
        # - deliberately, because a malformed order will not succeed on a
        # retry and redelivering it three times only delays the same answer.
        # The consequence is that the message never reaches the dead-letter
        # queue, never ages on the main queue and is not a Lambda error, so
        # alarms 1, 2 and 3 are all silent while a customer is told their
        # order could not be processed.
        #
        # The metric is the one the worker already emits, published with no
        # dimensions so an alarm can watch it (see observability/metrics.py).
        # It counts FAILED only. A clarification is recorded as a completed
        # order, because asking the shop owner which colour they meant is the
        # system working, not failing, and an alarm that fires on safe
        # behaviour is an alarm people learn to ignore.
        #
        # Threshold 0 over fifteen minutes: the right number of orders lost
        # inside the agent is none. Like every alarm here it publishes to the
        # owner-alerts topic, which reaches a person only once subscribed.
        #
        # A message that names nothing the shop sells - a greeting, a prompt
        # injection - is NOT counted: the worker records it as the NO_PRODUCT
        # outcome of ShopFlowOrdersCompleted. An evaluator's injection test
        # once put this alarm into ALARM for correct behaviour.
        orderAgentFailures_alarm = cloudwatch.Alarm(
            self, "OrderAgentFailures",
            alarm_name=f"{PREFIX}-order-agent-failures",
            alarm_description=(
                "An order failed inside the agent and was marked FAILED "
                "without ever reaching the dead-letter queue, so no other "
                "alarm sees it. Safe clarifications are NOT counted here. "
                "The jobId is on the EMF log line beside the metric."),
            metric=cloudwatch.Metric(
                namespace="ShopFlow",
                metric_name="ShopFlowOrdersFailed",
                period=Duration.minutes(15),
                statistic="Sum",
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        # Operational alarms notify the operational topic - never the owner's.
        alarm_action = cloudwatch_actions.SnsAction(ops_topic)
        for alarm in (ordersDlqNotEmpty_alarm, workerErrorsSustained_alarm,
                      ordersQueueBacklog_alarm, orderAgentFailures_alarm):
            alarm.add_alarm_action(alarm_action)

        # ------------------------------------------------------------------
        # Dashboard
        # ------------------------------------------------------------------
        # One page answering "is ShopFlow working right now", in the order
        # somebody would ask it: are orders completing, is the agent failing,
        # is the queue draining, and what has the business done today.
        #
        # Every widget reads a metric that already exists. Nothing here
        # publishes a new one, so the dashboard costs the $3/month a custom
        # dashboard costs and adds no per-metric charge at all.
        #
        # Deliberately not decorative. There is no widget for a number that
        # nobody would act on, and no business figure anywhere on it - a
        # quotation total in an operational dashboard is a business fact
        # leaking into a system with a different audience, which is the rule
        # observability/metrics.py exists to keep.
        def order_metric(name: str, outcome: str, label: str,
                         colour: str | None = None) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="ShopFlow", metric_name=name,
                dimensions_map={"JobType": "ORDER", "Outcome": outcome},
                statistic="Sum", period=Duration.minutes(5), label=label,
                color=colour,
            )

        def shopflow_metric(name: str, label: str, statistic: str = "Sum",
                            dimensions: dict | None = None,
                            colour: str | None = None) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="ShopFlow", metric_name=name,
                dimensions_map=dimensions or {},
                statistic=statistic, period=Duration.minutes(5), label=label,
                color=colour,
            )

        dashboard = cloudwatch.Dashboard(
            self, "Dashboard",
            dashboard_name=f"{PREFIX}-operations",
            default_interval=Duration.hours(3),
        )
        dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown=(
                    "# ShopFlow operations\n"
                    "Order outcomes, agent health, queue health and business "
                    "events. Counts only - no quotation totals, prices, "
                    "margins or customer data appear on this page.\n\n"
                    "**A clarification is not a failure.** ShopFlow asking "
                    "which variant the customer meant is the system working; "
                    "it is charted beside QUOTED, not beside FAILED."),
                width=24, height=3,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Order outcomes",
                left=[
                    order_metric("ShopFlowOrdersCompleted", "QUOTED",
                                 "Quoted", cloudwatch.Color.GREEN),
                    order_metric("ShopFlowOrdersCompleted",
                                 "NEEDS_CLARIFICATION", "Clarification asked",
                                 cloudwatch.Color.BLUE),
                    order_metric("ShopFlowOrdersFailed", "FAILED", "Failed",
                                 cloudwatch.Color.RED),
                    # A message naming nothing the shop sells. Answered, not
                    # failed - which is why it is not in the alarmed metric.
                    order_metric("ShopFlowOrdersCompleted", "NO_PRODUCT",
                                 "No product named", cloudwatch.Color.GREY),
                ],
                width=12, height=6,
            ),
            cloudwatch.GraphWidget(
                title="Agent failures (alarmed)",
                left=[shopflow_metric("ShopFlowOrdersFailed",
                                      "Agent failures (all job types)",
                                      colour=cloudwatch.Color.RED)],
                left_annotations=[cloudwatch.HorizontalAnnotation(
                    value=0, label="alarm threshold",
                    color=cloudwatch.Color.RED)],
                width=12, height=6,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Queue health",
                left=[
                    orders_queue.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(5), statistic="Maximum",
                        label="Queue depth"),
                    orders_dlq.metric_approximate_number_of_messages_visible(
                        period=Duration.minutes(5), statistic="Maximum",
                        label="Dead-letter queue",
                        color=cloudwatch.Color.RED),
                ],
                right=[orders_queue.metric_approximate_age_of_oldest_message(
                    period=Duration.minutes(5), statistic="Maximum",
                    label="Oldest message (s)"),
                    # Bedrock request-rate throttles, each one a retried
                    # attempt. The capacity signal behind a growing queue.
                    order_metric("ShopFlowWorkerFailures", "THROTTLED",
                                 "Bedrock throttled (retried)",
                                 cloudwatch.Color.ORANGE)],
                width=12, height=6,
            ),
            cloudwatch.GraphWidget(
                title="Processing time",
                left=[shopflow_metric("ShopFlowWorkerProcessingSeconds",
                                      "Average order (s)", statistic="Average",
                                      dimensions={"JobType": "ORDER"}),
                      shopflow_metric("ShopFlowWorkerProcessingSeconds",
                                      "Slowest order (s)", statistic="Maximum",
                                      dimensions={"JobType": "ORDER"})],
                width=12, height=6,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Business events",
                left=[
                    shopflow_metric("ShopFlowBusinessEvents",
                                    "Supplier price changed",
                                    dimensions={"EventType":
                                                "SupplierPriceChanged"}),
                    shopflow_metric("ShopFlowBusinessEvents", "Stockout",
                                    dimensions={"EventType":
                                                "StockoutDetected"}),
                    shopflow_metric("ShopFlowBusinessEvents", "Low margin",
                                    dimensions={"EventType":
                                                "LowMarginDetected"}),
                    shopflow_metric("ShopFlowBusinessEvents",
                                    "Purchase plan generated",
                                    dimensions={"EventType":
                                                "PurchasePlanGenerated"}),
                ],
                width=12, height=6,
            ),
            cloudwatch.GraphWidget(
                title="Documents read, and what failed to publish",
                left=[
                    shopflow_metric("ShopFlowDocumentsExtracted",
                                    "Read by Textract",
                                    dimensions={"Reader": "TEXTRACT"}),
                    shopflow_metric("ShopFlowDocumentsExtracted",
                                    "Read by Nova Pro",
                                    dimensions={"Reader": "NOVA_PRO"}),
                ],
                right=[
                    shopflow_metric("ShopFlowDocumentExtractionFailures",
                                    "Extraction failed",
                                    colour=cloudwatch.Color.RED),
                    shopflow_metric("ShopFlowEventPublishFailures",
                                    "Event publish failed",
                                    colour=cloudwatch.Color.ORANGE),
                ],
                width=12, height=6,
            ),
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
        CfnOutput(self, "OrdersQueueUrl", value=orders_queue.queue_url,
                  description="Durable queue between the API and the worker")
        CfnOutput(self, "OrdersDlqUrl", value=orders_dlq.queue_url,
                  description="Dead-letter queue - should always be empty")

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
