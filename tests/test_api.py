"""The public API: request validation and the async job lifecycle.

DynamoDB and Lambda are replaced by in-memory fakes. What matters here is that
the API validates before it queues, that a job moves through its states, and
that a job id is the only thing the browser ever needs to hold.
"""

from __future__ import annotations

import json

import pytest

import lambdas.api.handler as api


class FakeTable:
    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(key):
        return (key["PK"], key["SK"])

    def put_item(self, Item):
        self.items[self._key(Item)] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues):
        item = self.items.setdefault(self._key(Key), dict(Key))
        for placeholder, name in ExpressionAttributeNames.items():
            item[name] = ExpressionAttributeValues[f":{placeholder[1:]}"]


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append({
            "function": FunctionName,
            "type": InvocationType,
            "payload": json.loads(Payload.decode("utf-8")),
        })
        return {"StatusCode": 202}


@pytest.fixture
def api_env(monkeypatch):
    table, lam = FakeTable(), FakeLambda()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "shopflow-order-worker")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "lambda_client", lambda: lam)
    return table, lam


def post_order(body: dict):
    return {"routeKey": "POST /api/orders", "body": json.dumps(body)}


def body_of(response):
    return json.loads(response["body"])


# ---- 10. async job lifecycle ----

def test_submitting_an_order_returns_a_job_id_immediately(api_env):
    response = api.handler(post_order({"orderText": "20 Anchor switches"}), None)
    assert response["statusCode"] == 202
    payload = body_of(response)
    assert payload["status"] == "QUEUED"
    assert len(payload["jobId"]) == 32


def test_submitting_an_order_queues_the_worker_asynchronously(api_env):
    table, lam = api_env
    job_id = body_of(api.handler(post_order({"orderText": "20 switches"}), None))["jobId"]

    assert len(lam.invocations) == 1
    assert lam.invocations[0]["type"] == "Event"
    assert lam.invocations[0]["payload"] == {"jobId": job_id}


def test_job_moves_from_queued_to_done_and_carries_the_result(api_env):
    table, _ = api_env
    job_id = body_of(api.handler(post_order({"orderText": "20 switches"}), None))["jobId"]

    queued = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "pathParameters": {"jobId": job_id}}, None))
    assert queued["status"] == "QUEUED"

    table.items[(f"JOB#{job_id}", "META")].update({
        "status": "DONE",
        "result": json.dumps({"status": "QUOTED", "quote": {"total": 22306.48}}),
    })

    done = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "pathParameters": {"jobId": job_id}}, None))
    assert done["status"] == "DONE"
    assert done["result"]["quote"]["total"] == 22306.48


def test_job_record_carries_a_ttl(api_env):
    table, _ = api_env
    api.handler(post_order({"orderText": "20 switches"}), None)
    item = next(iter(table.items.values()))
    assert item["expiresAt"] > item["createdAt"]


def test_unknown_job_is_a_404(api_env):
    response = api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "pathParameters": {"jobId": "0" * 32}},
        None)
    assert response["statusCode"] == 404


def test_malformed_job_id_is_rejected(api_env):
    response = api.handler(
        {"routeKey": "GET /api/jobs/{jobId}",
         "pathParameters": {"jobId": "../../etc/passwd"}}, None)
    assert response["statusCode"] == 400


# ---- request validation, before anything is queued ----

def test_empty_order_is_rejected_without_queueing(api_env):
    _, lam = api_env
    assert api.handler(post_order({"orderText": "  "}), None)["statusCode"] == 400
    assert lam.invocations == []


def test_oversized_order_text_is_rejected(api_env):
    _, lam = api_env
    response = api.handler(post_order({"orderText": "x" * 1001}), None)
    assert response["statusCode"] == 400
    assert lam.invocations == []


def test_oversized_body_is_rejected(api_env):
    _, lam = api_env
    event = {"routeKey": "POST /api/orders", "body": "x" * 5000}
    assert api.handler(event, None)["statusCode"] == 413
    assert lam.invocations == []


def test_malformed_json_is_rejected(api_env):
    assert api.handler(
        {"routeKey": "POST /api/orders", "body": "{not json"}, None
    )["statusCode"] == 400


def test_clarification_with_an_invented_sku_is_rejected(api_env):
    """An invented SKU must not re-enter the flow through the clarification path."""
    _, lam = api_env
    response = api.handler(post_order({
        "orderText": "3 coils wire",
        "clarifications": [{"requestedText": "wire", "skuId": "MADE-UP"}],
    }), None)
    assert response["statusCode"] == 400
    assert "unknown skuId" in body_of(response)["error"]
    assert lam.invocations == []


def test_clarification_with_a_real_sku_is_accepted(api_env):
    table, _ = api_env
    response = api.handler(post_order({
        "orderText": "3 coils Finolex 1.5 wire",
        "clarifications": [{"requestedText": "wire", "skuId": "W-FIN-1.5-RED-90M"}],
    }), None)
    assert response["statusCode"] == 202
    item = next(iter(table.items.values()))
    assert item["clarifications"][0]["skuId"] == "W-FIN-1.5-RED-90M"


def test_control_characters_are_stripped_from_order_text(api_env):
    table, _ = api_env
    api.handler(post_order({"orderText": "20 switches\x00\x07 please"}), None)
    stored = next(iter(table.items.values()))["orderText"]
    assert "\x00" not in stored and "\x07" not in stored


def test_unknown_route_is_a_404(api_env):
    assert api.handler({"routeKey": "DELETE /api/everything"}, None)["statusCode"] == 404


def test_demo_route_exposes_example_inputs_but_no_totals(api_env):
    payload = body_of(api.handler({"routeKey": "GET /api/demo"}, None))
    assert payload["catalogSize"] == 147
    assert "Anchor" in payload["exampleOrder"]
    # The UI must fetch figures from the engine, never from this route.
    assert "total" not in payload and "quote" not in payload
