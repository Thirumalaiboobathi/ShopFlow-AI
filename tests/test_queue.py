"""The durable order queue: the defect it fixes, and the guarantees it adds.

WHAT WAS WRONG
--------------
The API used to hand a job to the worker with
`lambda.invoke(InvocationType="Event")`, and the worker was configured with
`retry_attempts=0`. If that invoke failed, nothing retried it and nothing
recorded it. The job row sat at QUEUED until its 24-hour TTL removed it, and
the browser polled a job that would never finish. An order was lost, silently,
with no way to find out.

WHAT IS TESTED HERE
-------------------
Four things, in the order they can go wrong:

  1. **Submission** - the job is persisted and a message with the right
     identifier reaches the queue, carrying no business data.
  2. **Send failure** - the API does not claim success, and does not leave an
     orphan job behind.
  3. **Idempotency** - SQS delivers at least once, so the worker is run twice
     on the same job and must not produce a second quotation.
  4. **Retry semantics** - a throttle is retried, an oversized order is not.

Plus the infrastructure itself. The redrive policy, the maxReceiveCount, the
bounded concurrency and the alarms are asserted against the synthesized
CloudFormation template rather than by trying to simulate SQS redrive locally,
which would test a simulation rather than the stack.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types

import pytest

import lambdas.api.handler as api
from observability import metrics
from test_api import FakeQueue, FakeS3, FakeTable, body_of, post_order

QUEUE_URL = "https://sqs.ap-south-1.amazonaws.com/000000000000/shopflow-orders"


@pytest.fixture
def api_env(monkeypatch):
    """The API with DynamoDB, SQS and S3 replaced. Its own copy.

    pytest fixtures are not importable across modules, so this mirrors the one
    in test_api.py rather than borrowing it.
    """
    table, queue, s3 = FakeTable(), FakeQueue(), FakeS3()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("ORDERS_QUEUE_URL", QUEUE_URL)
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "sqs_client", lambda: queue)
    monkeypatch.setattr(api, "s3_client", lambda: s3)
    return table, queue, s3


def one(result):
    """The single record's result out of a batch response.

    Batch size is 1 in the deployed stack, so every real invocation carries
    exactly one. The wrapper is kept in the handler because it is the honest
    shape for a batch; this unwraps it for readability here.
    """
    assert len(result["results"]) == 1
    return result["results"][0]


# ---------------------------------------------------------------------------
# 1. submission
# ---------------------------------------------------------------------------

def test_1_an_order_is_persisted_and_queued(api_env):
    table, queue, _ = api_env
    response = api.handler(post_order({"orderText": "20 Anchor switches"}), None)

    assert response["statusCode"] == 202
    body = body_of(response)
    assert body["status"] == "QUEUED"

    # The job exists...
    job = table.items[("JOB#" + body["jobId"], "META")]
    assert job["status"] == "QUEUED"
    assert job["jobType"] == "ORDER"

    # ...and exactly one message describes it.
    assert len(queue.messages) == 1
    assert queue.messages[0]["queueUrl"] == QUEUE_URL


def test_1a_the_message_carries_the_job_id_as_the_correlation_id(api_env):
    _, queue, _ = api_env
    body = body_of(api.handler(post_order({"orderText": "20 switches"}), None))
    assert queue.messages[0]["body"]["jobId"] == body["jobId"]


def test_1b_the_message_is_versioned(api_env):
    _, queue, _ = api_env
    api.handler(post_order({"orderText": "20 switches"}), None)
    assert queue.messages[0]["body"]["version"] == api.QUEUE_MESSAGE_VERSION


def test_1c_no_business_data_travels_through_the_queue(api_env):
    """The message is an identifier. The job record holds everything else.

    This matters for more than tidiness. A message that sat in the
    dead-letter queue would be readable by anyone with queue access, and a
    dead-lettered message that contained a customer's order text, their id or
    their language preference would be a disclosure sitting in an operational
    system for fourteen days.
    """
    _, queue, _ = api_env
    api.handler(post_order({
        "orderText": "20 Anchor switches for Ravi",
        "customerId": "CUST-RAVI-001",
        "language": "ta",
    }), None)
    body = queue.messages[0]["body"]
    assert set(body) == {"jobId", "jobType", "version"}
    serialised = json.dumps(body)
    for leak in ("Anchor", "Ravi", "CUST-RAVI-001", "orderText", "ta"):
        assert leak not in serialised, leak


def test_1d_a_price_list_is_queued_the_same_way(api_env):
    """Both asynchronous workflows go through the same queue."""
    from test_api import post_price_list

    _, queue, _ = api_env
    response = api.handler(post_price_list(), None)
    assert response["statusCode"] == 202
    assert len(queue.messages) == 1
    assert queue.messages[0]["body"]["jobType"] == "PRICE_LIST"
    assert set(queue.messages[0]["body"]) == {"jobId", "jobType", "version"}


def test_1e_a_rejected_request_queues_nothing(api_env):
    _, queue, _ = api_env
    api.handler(post_order({"orderText": ""}), None)
    assert queue.messages == []


# ---------------------------------------------------------------------------
# 2. the API no longer invokes the worker at all
# ---------------------------------------------------------------------------

def test_2_the_hand_rolled_async_invoke_is_gone():
    """Read from the source, so this cannot pass by accident.

    The old path is not deprecated or left behind a flag - it does not exist.
    The stack backs this up by removing the API role's
    `lambda:InvokeFunction`, which is what makes the queue the only route
    into the worker rather than merely the preferred one.
    """
    import ast

    source = open(api.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    # Docstrings and comments are stripped first. This module's own docstring
    # explains what `InvocationType="Event"` used to do and why it is gone,
    # and a test that failed on that explanation would be checking prose.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)

    for gone in ("InvocationType", "lambda_client", "_start_worker",
                 "WORKER_FUNCTION_NAME"):
        assert gone not in code, f"{gone} is still live code in the API"


def test_2a_the_api_module_builds_no_lambda_client():
    assert not hasattr(api, "lambda_client")
    assert hasattr(api, "sqs_client")


# ---------------------------------------------------------------------------
# 3. the send fails
# ---------------------------------------------------------------------------

def test_3_a_failed_send_is_never_reported_as_accepted(api_env):
    table, queue, _ = api_env
    queue.fail_next = True
    response = api.handler(post_order({"orderText": "20 switches"}), None)

    assert response["statusCode"] == 503
    body = body_of(response)
    assert "jobId" not in body
    assert "error" in body


def test_3a_a_failed_send_leaves_no_orphan_queued_job(api_env):
    """The compensating delete, tested.

    There is no transaction across DynamoDB and SQS. The row is written
    first, and if the send fails it is removed again - so the table does not
    accumulate jobs that nothing will ever process and that a browser could
    poll forever.
    """
    table, queue, _ = api_env
    queue.fail_next = True
    api.handler(post_order({"orderText": "20 switches"}), None)

    jobs = [k for k in table.items if str(k[0]).startswith("JOB#")]
    assert jobs == [], f"orphan job left behind: {jobs}"


def test_3b_a_failed_send_queues_nothing(api_env):
    _, queue, _ = api_env
    queue.fail_next = True
    api.handler(post_order({"orderText": "20 switches"}), None)
    assert queue.messages == []


def test_3c_a_failed_send_does_not_raise_through_the_route(api_env):
    """A queue outage is a 503, not a 500 and not a stack trace."""
    _, queue, _ = api_env
    queue.fail_next = True
    response = api.handler(post_order({"orderText": "20 switches"}), None)
    assert response["statusCode"] == 503
    assert "please try again" in body_of(response)["error"]


# ---------------------------------------------------------------------------
# 4. the worker
# ---------------------------------------------------------------------------

class WorkerTable:
    """A DynamoDB stand-in that honours the one condition the worker relies on."""

    def __init__(self, item=None):
        self.item = dict(item) if item else None
        self.updates = []
        self.deleted = False

    def get_item(self, Key):
        return {"Item": dict(self.item)} if self.item else {}

    def update_item(self, Key, UpdateExpression, **kwargs):
        condition = kwargs.get("ConditionExpression") or ""
        values = kwargs.get("ExpressionAttributeValues") or {}
        if "#status IN" in condition:
            from botocore.exceptions import ClientError
            allowed = {values.get(":queued"), values.get(":processing")}
            if self.item is None or self.item.get("status") not in allowed:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem")
            self.item["status"] = values[":processing"]
            self.item["attempts"] = self.item.get("attempts", 0) + 1
            self.updates.append({"claim": True})
            return {}
        # A plain SET update from _update()
        fields = {k.lstrip(":"): v for k, v in values.items()}
        self.item = {**(self.item or {}), **fields}
        self.updates.append(fields)
        return {}

    def query(self, **kwargs):
        return {"Items": []}


@pytest.fixture
def worker(monkeypatch):
    """The worker module, with DynamoDB and the agent replaced."""
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")

    import boto3

    class _Res:
        def Table(self, name):
            return WorkerTable()

    monkeypatch.setattr(boto3, "resource", lambda *a, **k: _Res())
    sys.modules.pop("lambdas.worker.handler", None)
    module = importlib.import_module("lambdas.worker.handler")
    return module


def sqs_event(body):
    return {"Records": [{"messageId": "m-1", "body": json.dumps(body)}]}


def test_4_a_valid_message_processes_the_existing_job(worker, monkeypatch):
    table = WorkerTable({"jobId": "a" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "20 switches"})
    monkeypatch.setattr(worker, "_table", table)

    calls = []

    def fake_agent(data, text, **kwargs):
        calls.append(text)
        return types.SimpleNamespace(
            status="QUOTED", turns=1, grounded=True, modelId="m",
            as_dict=lambda: {"status": "QUOTED", "quote": None})

    monkeypatch.setattr(worker, "run_order_agent", fake_agent)
    monkeypatch.setattr(worker, "check_quote_credit", lambda *a, **k: None)

    result = worker.handler(sqs_event({"jobId": "a" * 32}), None)
    assert one(result)["ok"] is True
    assert len(calls) == 1
    assert table.item["status"] == "DONE"


def test_4a_a_malformed_message_body_fails_safely(worker):
    """Unparseable JSON is acknowledged, not retried three times."""
    result = worker.handler(
        {"Records": [{"messageId": "m-1", "body": "{not json"}]}, None)
    assert result["ok"] is False


def test_4b_a_message_with_no_job_id_fails_safely(worker):
    result = worker.handler(sqs_event({"nothing": "here"}), None)
    assert one(result)["ok"] is False


def test_4c_an_unknown_job_id_fails_safely_and_is_not_retried(worker,
                                                              monkeypatch):
    """A job that expired, or one the API's compensating delete removed.

    Neither improves on a retry, so the message is acknowledged rather than
    dead-lettered.
    """
    monkeypatch.setattr(worker, "_table", WorkerTable(None))
    result = one(worker.handler(sqs_event({"jobId": "b" * 32}), None))
    assert result["ok"] is False
    assert result["reason"] == "unknown-job"


# ---------------------------------------------------------------------------
# 5. idempotency - the point of the whole exercise
# ---------------------------------------------------------------------------

def test_5_a_duplicate_delivery_for_a_finished_job_does_not_re_run(
        worker, monkeypatch):
    """SQS delivers at least once. Twice must cost nothing.

    The agent is the expensive, side-effecting part: a second run is a second
    Bedrock call that could return a DIFFERENT quotation and overwrite the one
    the customer was already shown. The conditional claim is what prevents it.
    """
    table = WorkerTable({
        "jobId": "c" * 32, "jobType": "ORDER", "status": "DONE",
        "result": json.dumps({"status": "QUOTED", "quote": {"total": 22306.48}}),
    })
    monkeypatch.setattr(worker, "_table", table)

    calls = []
    monkeypatch.setattr(worker, "run_order_agent",
                        lambda *a, **k: calls.append(1))

    result = one(worker.handler(sqs_event({"jobId": "c" * 32}), None))

    assert result["ok"] is True
    assert result["duplicate"] is True
    assert calls == [], "the agent ran again on a completed job"


def test_5a_a_duplicate_delivery_does_not_change_the_quotation(worker,
                                                               monkeypatch):
    stored = json.dumps({"status": "QUOTED", "quote": {"total": 22306.48}})
    table = WorkerTable({"jobId": "c" * 32, "jobType": "ORDER",
                         "status": "DONE", "result": stored})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent",
                        lambda *a, **k: pytest.fail("agent must not run"))

    worker.handler(sqs_event({"jobId": "c" * 32}), None)
    assert table.item["result"] == stored
    assert json.loads(table.item["result"])["quote"]["total"] == 22306.48


def test_5b_a_duplicate_delivery_writes_nothing_at_all(worker, monkeypatch):
    table = WorkerTable({"jobId": "c" * 32, "jobType": "ORDER",
                         "status": "DONE"})
    monkeypatch.setattr(worker, "_table", table)
    worker.handler(sqs_event({"jobId": "c" * 32}), None)
    assert table.updates == [], "a finished job was written to again"


def test_5c_a_failed_job_is_also_not_re_run(worker, monkeypatch):
    """FAILED is an answer too. Redelivering it must not restart the work."""
    table = WorkerTable({"jobId": "d" * 32, "jobType": "ORDER",
                         "status": "FAILED"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent",
                        lambda *a, **k: pytest.fail("agent must not run"))
    result = one(worker.handler(sqs_event({"jobId": "d" * 32}), None))
    assert result["duplicate"] is True


def test_5d_a_genuine_retry_of_an_abandoned_job_is_allowed(worker, monkeypatch):
    """PROCESSING is NOT terminal.

    A worker that timed out leaves the job at PROCESSING. When SQS redelivers
    it the claim must succeed, or a transient timeout would strand the job
    permanently - which is the exact failure this whole change exists to
    remove.
    """
    table = WorkerTable({"jobId": "e" * 32, "jobType": "ORDER",
                         "status": "PROCESSING", "orderText": "20 switches"})
    monkeypatch.setattr(worker, "_table", table)

    calls = []
    monkeypatch.setattr(worker, "run_order_agent", lambda *a, **k: (
        calls.append(1) or types.SimpleNamespace(
            status="QUOTED", turns=1, grounded=True, modelId="m",
            as_dict=lambda: {"status": "QUOTED", "quote": None})))
    monkeypatch.setattr(worker, "check_quote_credit", lambda *a, **k: None)

    worker.handler(sqs_event({"jobId": "e" * 32}), None)
    assert calls == [1], "a retry of an abandoned job was refused"


def test_5e_the_attempt_count_is_recorded(worker, monkeypatch):
    table = WorkerTable({"jobId": "f" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "x"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent", lambda *a, **k:
                        types.SimpleNamespace(
                            status="QUOTED", turns=1, grounded=True, modelId="m",
                            as_dict=lambda: {"status": "QUOTED", "quote": None}))
    monkeypatch.setattr(worker, "check_quote_credit", lambda *a, **k: None)
    worker.handler(sqs_event({"jobId": "f" * 32}), None)
    assert table.item["attempts"] == 1


# ---------------------------------------------------------------------------
# 6. retryable versus terminal
# ---------------------------------------------------------------------------

def _client_error(code, status=400):
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse")


@pytest.mark.parametrize("code", [
    "ThrottlingException", "TooManyRequestsException", "ServiceUnavailable",
    "InternalServerException", "ModelTimeoutException",
])
def test_6_a_throttle_or_a_5xx_is_retryable(worker, code):
    assert worker.is_retryable(_client_error(code)) is True


@pytest.mark.parametrize("code", [
    "ValidationException", "AccessDeniedException",
    "ResourceNotFoundException",
])
def test_6a_a_client_error_is_terminal(worker, code):
    assert worker.is_retryable(_client_error(code)) is False


def test_6b_any_5xx_is_retryable_even_with_an_unknown_code(worker):
    assert worker.is_retryable(_client_error("SomethingNew", 503)) is True


def test_6c_a_plain_exception_is_terminal(worker):
    """The safe default.

    A retryable failure wrongly called terminal loses one order and says so.
    A terminal failure wrongly retried runs the Bedrock loop three times and
    still fails. Defaulting to terminal is the cheaper mistake.
    """
    assert worker.is_retryable(ValueError("nope")) is False


def test_6d_a_retryable_failure_is_raised_so_sqs_redelivers(worker, monkeypatch):
    table = WorkerTable({"jobId": "g" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "20 switches"})
    monkeypatch.setattr(worker, "_table", table)

    def throttled(*a, **k):
        raise _client_error("ThrottlingException", 429)

    monkeypatch.setattr(worker, "run_order_agent", throttled)

    with pytest.raises(Exception) as caught:
        worker.handler(sqs_event({"jobId": "g" * 32}), None)
    assert "Throttling" in str(caught.value)


def test_6e_a_retryable_failure_does_not_mark_the_job_failed(worker,
                                                             monkeypatch):
    """The job stays recoverable. This is the heart of the failure semantics.

    Marking a throttled order FAILED would turn a two-second AWS blip into a
    lost customer order - exactly the outcome the queue was introduced to
    prevent.
    """
    table = WorkerTable({"jobId": "h" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "20 switches"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent", lambda *a, **k: (_ for _ in ()
                        ).throw(_client_error("ThrottlingException", 429)))

    with pytest.raises(Exception):
        worker.handler(sqs_event({"jobId": "h" * 32}), None)

    assert table.item["status"] == "PROCESSING"
    assert table.item.get("error") is None


def test_6f_a_terminal_failure_marks_the_job_failed_and_is_acknowledged(
        worker, monkeypatch):
    table = WorkerTable({"jobId": "i" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "20 switches"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent", lambda *a, **k: (_ for _ in ()
                        ).throw(_client_error("ValidationException", 400)))

    result = one(worker.handler(sqs_event({"jobId": "i" * 32}), None))
    assert result["ok"] is False
    assert table.item["status"] == "FAILED"


def test_6g_an_oversized_order_is_terminal_not_retried(worker, monkeypatch):
    table = WorkerTable({"jobId": "j" * 32, "jobType": "ORDER",
                         "status": "QUEUED", "orderText": "x"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "run_order_agent", lambda *a, **k: (_ for _ in ()
                        ).throw(worker.OrderTooLongError("too long")))

    result = one(worker.handler(sqs_event({"jobId": "j" * 32}), None))
    assert result["ok"] is False
    assert table.item["status"] == "FAILED"


# ---------------------------------------------------------------------------
# 7. metrics carry no business value
# ---------------------------------------------------------------------------

def test_7_a_metric_outside_the_allow_list_is_dropped():
    assert metrics.emit("SomeMetricNobodyDeclared", 1) is None


def test_7a_only_safe_dimensions_survive():
    document = metrics.emit(metrics.ORDERS_QUEUED, 1, dimensions={
        "JobType": "ORDER",
        "CustomerId": "CUST-RAVI-001",
        "Total": "22306.48",
    })
    assert document["JobType"] == "ORDER"
    assert "CustomerId" not in document
    assert "Total" not in document


def test_7b_an_out_of_range_dimension_value_is_dropped():
    """Cardinality is bounded by construction, not by care.

    Every distinct dimension value is a separate billed metric, so an
    unbounded value here would be a slowly growing bill as well as a leak.
    """
    document = metrics.emit(metrics.ORDERS_QUEUED, 1,
                            dimensions={"JobType": "CUST-RAVI-001"})
    assert "JobType" not in document


def test_7c_the_job_id_is_a_property_not_a_dimension():
    document = metrics.emit(metrics.ORDERS_QUEUED, 1, job_id="a" * 32,
                            dimensions={"JobType": "ORDER"})
    dimensions = document["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]
    assert "jobId" not in dimensions
    assert document["jobId"] == "a" * 32


def test_7d_emitting_never_raises():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    assert metrics.emit(metrics.ORDERS_QUEUED, 1,
                        dimensions={"JobType": Hostile()}) is None


def test_7e_no_metric_name_suggests_a_business_figure():
    names = [v for k, v in vars(metrics).items()
             if k.isupper() and isinstance(v, str) and v.startswith("ShopFlow")]
    assert names
    for name in names:
        lowered = name.lower()
        for banned in ("total", "price", "margin", "credit", "cost", "rupee",
                       "amount", "quote"):
            assert banned not in lowered, f"{name} sounds like a business value"


def test_7f_the_metrics_module_imports_no_business_engine():
    source = open(metrics.__file__, encoding="utf-8").read()
    for banned in ("engine", "boto3", "quote", "credit", "pricing"):
        assert f"import {banned}" not in source
        assert f"from {banned}" not in source


# ---------------------------------------------------------------------------
# 8. the infrastructure itself
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def template():
    """The synthesized CloudFormation template.

    Redrive behaviour is asserted here rather than simulated. A local
    simulation of SQS would be testing the simulation; the template is what
    AWS will actually be asked to build.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "infrastructure"))
    import aws_cdk
    from aws_cdk.assertions import Template

    from shopflow_stack import ShopFlowStack

    # `Code.from_asset("../backend")` is relative to the working directory,
    # and the app is normally run from infrastructure/. Synthesizing from
    # anywhere else cannot find the asset.
    previous = os.getcwd()
    os.chdir(os.path.join(root, "infrastructure"))
    try:
        app = aws_cdk.App()
        stack = ShopFlowStack(
            app, "ShopFlowStack",
            alert_email="test@example.com",
            env=aws_cdk.Environment(account="675613597178",
                                    region="ap-south-1"))
        return Template.from_stack(stack)
    finally:
        os.chdir(previous)


def _resources(template, kind):
    return list(template.find_resources(kind).values())


def test_8_there_is_exactly_one_queue_and_one_dead_letter_queue(template):
    queues = _resources(template, "AWS::SQS::Queue")
    assert len(queues) == 2, [q["Properties"].get("QueueName") for q in queues]
    names = {q["Properties"]["QueueName"] for q in queues}
    assert names == {"shopflow-orders", "shopflow-orders-dlq"}


def test_8a_the_queue_has_a_redrive_policy_pointing_at_the_dlq(template):
    main = [q for q in _resources(template, "AWS::SQS::Queue")
            if q["Properties"]["QueueName"] == "shopflow-orders"][0]
    redrive = main["Properties"]["RedrivePolicy"]
    assert redrive["maxReceiveCount"] == 3
    # The ARN is a CloudFormation reference to the DLQ resource, not a string.
    assert "deadLetterTargetArn" in redrive
    assert "Fn::GetAtt" in json.dumps(redrive["deadLetterTargetArn"])


def test_8b_the_visibility_timeout_is_six_times_the_worker_timeout(template):
    main = [q for q in _resources(template, "AWS::SQS::Queue")
            if q["Properties"]["QueueName"] == "shopflow-orders"][0]
    worker = [f for f in _resources(template, "AWS::Lambda::Function")
              if f["Properties"].get("FunctionName") == "shopflow-order-worker"][0]
    assert main["Properties"]["VisibilityTimeout"] == \
        worker["Properties"]["Timeout"] * 6


def test_8c_both_queues_are_encrypted_and_require_tls(template):
    for queue in _resources(template, "AWS::SQS::Queue"):
        assert queue["Properties"].get("SqsManagedSseEnabled") is True
    policies = _resources(template, "AWS::SQS::QueuePolicy")
    assert len(policies) == 2
    assert "aws:SecureTransport" in json.dumps(policies)


def test_8d_the_dlq_retains_messages_for_fourteen_days(template):
    dlq = [q for q in _resources(template, "AWS::SQS::Queue")
           if q["Properties"]["QueueName"] == "shopflow-orders-dlq"][0]
    assert dlq["Properties"]["MessageRetentionPeriod"] == 14 * 24 * 3600


def test_8e_the_worker_consumes_the_queue_with_bounded_concurrency(template):
    """The account ceiling is 10. An unbounded queue burst would starve the API.

    This is the assertion that protects the public site from its own backlog.
    """
    mappings = _resources(template, "AWS::Lambda::EventSourceMapping")
    assert len(mappings) == 1
    properties = mappings[0]["Properties"]
    assert properties["BatchSize"] == 1
    # Two: the account's Bedrock quota (25 Nova Pro requests a minute) is the
    # real ceiling, and above two workers extra concurrency only converts
    # queued orders into throttled ones. Two is the lowest value SQS accepts.
    assert properties["ScalingConfig"]["MaximumConcurrency"] == 2


def test_8f_bounded_concurrency_leaves_room_for_the_api(template):
    mapping = _resources(template, "AWS::Lambda::EventSourceMapping")[0]
    ACCOUNT_CONCURRENCY_CEILING = 10
    assert mapping["Properties"]["ScalingConfig"]["MaximumConcurrency"] < \
        ACCOUNT_CONCURRENCY_CEILING


def test_8g_no_function_reserves_concurrency(template):
    """Reserving is rejected on an account with a ceiling of 10, and would
    take capacity from other projects rather than capping this one."""
    for fn in _resources(template, "AWS::Lambda::Function"):
        assert "ReservedConcurrentExecutions" not in fn["Properties"]


def test_8h_there_are_four_alarms_each_with_a_description(template):
    alarms = _resources(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 4
    names = {a["Properties"]["AlarmName"] for a in alarms}
    assert names == {
        "shopflow-orders-dlq-not-empty",
        "shopflow-worker-errors-sustained",
        "shopflow-orders-queue-backlog",
        # An agent failure never reaches the dead-letter queue, so the first
        # three alarms are silent while a customer is told their order could
        # not be processed. This is the one that sees it.
        "shopflow-order-agent-failures",
    }
    for alarm in alarms:
        assert len(alarm["Properties"]["AlarmDescription"]) > 40


def test_8h2_the_agent_failure_alarm_watches_the_metric_the_worker_emits(template):
    """The alarm and the emitter must agree on the name, or it watches nothing."""
    alarm = [a for a in _resources(template, "AWS::CloudWatch::Alarm")
             if a["Properties"]["AlarmName"] == "shopflow-order-agent-failures"][0]
    assert alarm["Properties"]["Namespace"] == metrics.NAMESPACE
    assert alarm["Properties"]["MetricName"] == metrics.ORDERS_FAILED
    # Undimensioned, which is the only shape an alarm can watch.
    assert not alarm["Properties"].get("Dimensions")
    assert alarm["Properties"]["Threshold"] == 0
    assert metrics.ORDERS_FAILED in metrics._AGGREGATE_METRICS


def test_8h3_a_safe_clarification_is_not_counted_as_a_failure(template):
    """An alarm that fires on correct behaviour is one people learn to ignore."""
    failed = metrics.emit(metrics.ORDERS_FAILED,
                          dimensions={"JobType": "ORDER", "Outcome": "FAILED"})
    clarified = metrics.emit(metrics.ORDERS_COMPLETED,
                             dimensions={"JobType": "ORDER",
                                         "Outcome": "NEEDS_CLARIFICATION"})

    sets = failed["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert [] in sets                      # the alarm can see this one
    assert metrics.ORDERS_FAILED in failed
    # A clarification is a completed order and is published only under its
    # own breakdown, so it can never reach the failure alarm.
    assert metrics.ORDERS_FAILED not in clarified
    assert [] not in clarified["_aws"]["CloudWatchMetrics"][0]["Dimensions"]


def test_8i_the_dlq_alarm_fires_on_a_single_message(template):
    """Zero is the correct number of dead-lettered orders.

    This is not a rate to tune. One message means a customer's order failed
    three times and nothing will retry it.
    """
    alarm = [a for a in _resources(template, "AWS::CloudWatch::Alarm")
             if a["Properties"]["AlarmName"] == "shopflow-orders-dlq-not-empty"][0]
    assert alarm["Properties"]["Threshold"] == 0
    assert alarm["Properties"]["EvaluationPeriods"] == 1
    assert alarm["Properties"]["ComparisonOperator"] == "GreaterThanThreshold"


def test_8j_every_alarm_notifies_the_owner_alerts_topic(template):
    """Every alarm has an action, and the action is the stack's one topic.

    This used to assert the opposite - that no alarm notified anybody - and an
    independent evaluator rightly called the alarms decorative. The topic is
    the existing owner-alerts topic; no second topic and no subscription are
    created here, so deploying still emails nobody until someone subscribes.
    """
    topics = template.find_resources("AWS::SNS::Topic")
    assert {t["Properties"]["TopicName"] for t in topics.values()} == {
        "shopflow-owner-alerts"}
    topic_id = next(iter(topics))

    alarms = _resources(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 4
    for alarm in alarms:
        assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_id}],             alarm["Properties"]["AlarmName"]


def test_8j1_cloudwatch_may_publish_to_the_topic(template):
    """An alarm action is refused at publish time unless the topic policy
    admits CloudWatch.

    The EventBridge target replaces SNS's default topic policy with one that
    admits events.amazonaws.com only - which is exactly the policy on the live
    topic today. Both principals must be present, CloudWatch's scoped to this
    account's ShopFlow alarms.
    """
    policies = _resources(template, "AWS::SNS::TopicPolicy")
    statements = [st for p in policies
                  for st in p["Properties"]["PolicyDocument"]["Statement"]]
    principals = {st["Principal"]["Service"] for st in statements
                  if st.get("Effect") == "Allow"
                  and "sns:Publish" in json.dumps(st.get("Action"))}
    assert {"events.amazonaws.com", "cloudwatch.amazonaws.com"} <= principals

    cloudwatch = [st for st in statements
                  if st["Principal"]["Service"] == "cloudwatch.amazonaws.com"][0]
    condition = json.dumps(cloudwatch["Condition"])
    assert "aws:SourceAccount" in condition
    assert ":alarm:shopflow-*" in condition


def test_8j2_the_owner_alert_topic_has_no_subscription_by_default(template):
    """Deploying must not sign anybody up for email.

    `enableBusinessAlerts` is off unless it is deliberately set, so the
    default template carries a topic with nothing subscribed to it. Events
    are still published and still routed; they simply reach nobody, which is
    the right default for a public demo.
    """
    assert _resources(template, "AWS::SNS::Subscription") == []


def test_8j3_no_email_address_is_written_into_the_stack_source():
    """An address belongs in context, never in a file that gets committed."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "infrastructure" / "shopflow_stack.py").read_text(
        encoding="utf-8")
    assert "@" not in source.replace("@example", "").replace(
        "aws-cdk-lib", "")


def test_8k_the_budget_survives(template):
    budgets = _resources(template, "AWS::Budgets::Budget")
    assert len(budgets) == 1
    assert budgets[0]["Properties"]["Budget"]["BudgetLimit"]["Amount"] == 25


def test_8l_no_service_is_added_that_nothing_uses(template):
    """The list that matters, minus the two this build deliberately added.

    EventBridge and SNS were added on purpose: business events are routed to
    a bus, and the alert-worthy ones reach an owner topic. They are asserted
    positively in the two tests below, so removing them from this list does
    not leave them unchecked.

    Everything still forbidden is something nothing in ShopFlow uses, and the
    point of the test is unchanged - a service may not appear because it
    sounds impressive. Step Functions in particular: the document reader is a
    single synchronous Textract call and needs no state machine.
    """
    kinds = {r["Type"] for r in template.to_json()["Resources"].values()}
    for forbidden in ("AWS::StepFunctions::StateMachine",
                      "AWS::WAFv2::WebACL", "AWS::KMS::Key",
                      "AWS::CloudTrail::Trail", "AWS::Cognito::UserPool",
                      "AWS::OpenSearchServerless::Collection",
                      "AWS::RDS::DBInstance", "AWS::EC2::NatGateway",
                      "AWS::Bedrock::Agent", "AWS::ElasticLoadBalancingV2::LoadBalancer"):
        assert forbidden not in kinds, f"{forbidden} was added"


def test_8m_there_is_exactly_one_bus_one_topic_and_one_rule(template):
    """Small on purpose. One bus, one topic, one routing rule between them -
    and one schedule, for the daily brief, which cannot live on a custom bus."""
    buses = _resources(template, "AWS::Events::EventBus")
    rules = _resources(template, "AWS::Events::Rule")
    topics = _resources(template, "AWS::SNS::Topic")

    assert [b["Properties"]["Name"] for b in buses] == \
        ["shopflow-business-events"]
    assert [t["Properties"]["TopicName"] for t in topics] == \
        ["shopflow-owner-alerts"]
    routing = [r for r in rules if "EventPattern" in r["Properties"]]
    schedules = [r for r in rules if "ScheduleExpression" in r["Properties"]]
    assert len(routing) == 1
    assert len(schedules) == 1
    assert len(rules) == 2

    rule = routing[0]["Properties"]
    assert rule["EventPattern"]["source"] == ["shopflow.business"]
    # A clarification is correct behaviour and must not become an email.
    assert "OrderNeedsClarification" not in rule["EventPattern"]["detail-type"]
    assert set(rule["EventPattern"]["detail-type"]) == {
        "SupplierPriceChanged", "StockoutDetected", "LowMarginDetected",
        "OrderProcessingFailed", "PurchasePlanGenerated",
        "DailyShopBriefGenerated"}


def test_8m2_the_daily_brief_schedule_targets_only_the_worker(template):
    """One daily schedule, one target: the existing worker, with a constant
    input the worker recognises. No new function was added for it."""
    import json as _json

    schedule = next(r["Properties"] for r in
                    _resources(template, "AWS::Events::Rule")
                    if "ScheduleExpression" in r["Properties"])
    assert schedule["ScheduleExpression"].startswith("cron(")
    assert len(schedule["Targets"]) == 1
    target = schedule["Targets"][0]
    assert _json.loads(target["Input"]) == {"shopflowTask": "DAILY_BRIEF"}
    assert "WorkerFunction" in _json.dumps(target["Arn"])
    functions = [f for f in _resources(template, "AWS::Lambda::Function")
                 if str(f["Properties"].get("FunctionName", "")).startswith(
                     "shopflow-")]
    assert sorted(f["Properties"]["FunctionName"] for f in functions) == [
        "shopflow-api", "shopflow-health", "shopflow-order-worker"]


def test_8n_the_dashboard_is_operational_not_decorative(template):
    """One dashboard, and no business figures anywhere on it."""
    import json as _json

    dashboards = _resources(template, "AWS::CloudWatch::Dashboard")
    assert len(dashboards) == 1
    assert dashboards[0]["Properties"]["DashboardName"] == "shopflow-operations"

    body = _json.dumps(dashboards[0]["Properties"]["DashboardBody"])
    for forbidden in ("22306", "24993", "24996", "sellingPrice", "costPrice",
                      "creditLimit", "margin\\u20b9", "Revenue"):
        assert forbidden not in body, forbidden
    # It charts what the application already emits, and nothing else.
    assert "ShopFlowOrdersCompleted" in body
    assert "ShopFlowOrdersFailed" in body
    assert "ShopFlowBusinessEvents" in body


# ---------------------------------------------------------------------------
# 9. IAM
# ---------------------------------------------------------------------------

def test_9_the_api_can_send_to_the_queue_and_nothing_more(template):
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "sqs:SendMessage" in policies
    for forbidden in ("sqs:*", "sqs:ReceiveMessage\", \"sqs:DeleteQueue",
                      "sqs:PurgeQueue", "sqs:SetQueueAttributes"):
        assert forbidden not in policies, forbidden


def test_9a_the_api_can_no_longer_invoke_the_worker(template):
    """The old path is removed in IAM as well as in code.

    Deleting the call site is a decision. Removing the permission is a
    guarantee.
    """
    policies = template.find_resources("AWS::IAM::Policy")
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            assert "lambda:InvokeFunction" not in actions, \
                "the API can still invoke the worker directly"


def test_9b_no_policy_anywhere_uses_a_wildcard_action(template):
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            for action in actions:
                if isinstance(action, str):
                    assert not action.endswith(":*"), action
                    assert action != "*", action


def test_6h_a_price_list_terminal_failure_is_worded_for_a_price_list(
        worker, monkeypatch):
    """The owner is told which of their jobs failed, not a generic one.

    This message is returned by GET /api/jobs/{id} and rendered in the UI. A
    price list failing on a non-extraction error used to report "order
    processing failed", which is the wrong noun for the thing the owner just
    uploaded.
    """
    table = WorkerTable({"jobId": "k" * 32, "jobType": "PRICE_LIST",
                         "status": "QUEUED", "imageKey": "price-lists/x.png"})
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setattr(worker, "boto3", types.SimpleNamespace(
        client=lambda *a, **k: (_ for _ in ()).throw(
            _client_error("AccessDenied", 403))))

    result = one(worker.handler(sqs_event({"jobId": "k" * 32}), None))
    assert result["ok"] is False
    assert table.item["status"] == "FAILED"
    assert table.item["error"] == "price list processing failed"


def test_6i_both_job_types_are_claimed_before_any_work(worker, monkeypatch):
    """The idempotency guard sits in the dispatcher, not in one branch.

    If `_claim` had been left inside the order path, a duplicate price-list
    delivery would have re-run a Bedrock vision call. It is in
    `_process_message`, above the branch, so both types are covered by
    construction.
    """
    for job_type in ("ORDER", "PRICE_LIST"):
        table = WorkerTable({"jobId": "m" * 32, "jobType": job_type,
                             "status": "DONE"})
        monkeypatch.setattr(worker, "_table", table)
        result = one(worker.handler(sqs_event({"jobId": "m" * 32}), None))
        assert result["duplicate"] is True, job_type
        assert table.updates == [], f"{job_type} wrote to a finished job"
