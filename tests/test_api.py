"""The public API: request validation and the async job lifecycle.

DynamoDB and Lambda are replaced by in-memory fakes. What matters here is that
the API validates before it queues, that a job moves through its states, and
that a job id is the only thing the browser ever needs to hold.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

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


    def query(self, KeyConditionExpression):
        # Only the one access pattern the API uses: everything under one PK.
        pk = KeyConditionExpression._values[1]
        return {"Items": [dict(v) for k, v in self.items.items() if k[0] == pk]}


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


class FakeS3:
    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {}


@pytest.fixture
def api_env(monkeypatch):
    table, lam, s3 = FakeTable(), FakeLambda(), FakeS3()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "shopflow-order-worker")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "lambda_client", lambda: lam)
    monkeypatch.setattr(api, "s3_client", lambda: s3)
    return table, lam, s3


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
    table, lam, _s3 = api_env
    job_id = body_of(api.handler(post_order({"orderText": "20 switches"}), None))["jobId"]

    assert len(lam.invocations) == 1
    assert lam.invocations[0]["type"] == "Event"
    assert lam.invocations[0]["payload"] == {"jobId": job_id}


def test_job_moves_from_queued_to_done_and_carries_the_result(api_env):
    table, _lam, _s3 = api_env
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
    table, _lam, _s3 = api_env
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
    _t, lam, _s3 = api_env
    assert api.handler(post_order({"orderText": "  "}), None)["statusCode"] == 400
    assert lam.invocations == []


def test_oversized_order_text_is_rejected(api_env):
    _t, lam, _s3 = api_env
    response = api.handler(post_order({"orderText": "x" * 1001}), None)
    assert response["statusCode"] == 400
    assert lam.invocations == []


def test_oversized_body_is_rejected(api_env):
    _t, lam, _s3 = api_env
    event = {"routeKey": "POST /api/orders", "body": "x" * 5000}
    assert api.handler(event, None)["statusCode"] == 413
    assert lam.invocations == []


def test_malformed_json_is_rejected(api_env):
    assert api.handler(
        {"routeKey": "POST /api/orders", "body": "{not json"}, None
    )["statusCode"] == 400


def test_clarification_with_an_invented_sku_is_rejected(api_env):
    """An invented SKU must not re-enter the flow through the clarification path."""
    _t, lam, _s3 = api_env
    response = api.handler(post_order({
        "orderText": "3 coils wire",
        "clarifications": [{"requestedText": "wire", "skuId": "MADE-UP"}],
    }), None)
    assert response["statusCode"] == 400
    assert "unknown skuId" in body_of(response)["error"]
    assert lam.invocations == []


def test_clarification_with_a_real_sku_is_accepted(api_env):
    table, _lam, _s3 = api_env
    response = api.handler(post_order({
        "orderText": "3 coils Finolex 1.5 wire",
        "clarifications": [{"requestedText": "wire", "skuId": "W-FIN-1.5-RED-90M"}],
    }), None)
    assert response["statusCode"] == 202
    item = next(iter(table.items.values()))
    assert item["clarifications"][0]["skuId"] == "W-FIN-1.5-RED-90M"


def test_control_characters_are_stripped_from_order_text(api_env):
    table, _lam, _s3 = api_env
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


# ---- supplier price lists ----

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pretend image data"


def post_price_list(image: bytes = PNG_BYTES, content_type: str = "image/png",
                    encoded: str | None = None):
    payload = {
        "contentType": content_type,
        "imageBase64": encoded if encoded is not None
        else base64.b64encode(image).decode("ascii"),
    }
    return {"routeKey": "POST /api/supplier-price-lists",
            "body": json.dumps(payload)}


def test_uploading_a_price_list_returns_a_job_id(api_env):
    response = api.handler(post_price_list(), None)
    assert response["statusCode"] == 202
    payload = body_of(response)
    assert payload["jobType"] == "PRICE_LIST"
    assert len(payload["jobId"]) == 32


def test_uploaded_image_is_stored_privately_and_encrypted(api_env):
    _t, _lam, s3 = api_env
    job_id = body_of(api.handler(post_price_list(), None))["jobId"]

    assert len(s3.objects) == 1
    stored = s3.objects[0]
    assert stored["Bucket"] == "shopflow-uploads-test"
    assert stored["Key"] == f"price-lists/{job_id}.png"
    assert stored["Body"] == PNG_BYTES
    assert stored["ServerSideEncryption"] == "AES256"
    # Nothing that would make the object publicly readable.
    assert "ACL" not in stored


def test_uploading_a_price_list_queues_the_worker(api_env):
    _t, lam, _s3 = api_env
    job_id = body_of(api.handler(post_price_list(), None))["jobId"]
    assert lam.invocations[0]["type"] == "Event"
    assert lam.invocations[0]["payload"] == {"jobId": job_id}


def test_price_list_job_records_its_type(api_env):
    table, _lam, _s3 = api_env
    api.handler(post_price_list(), None)
    item = next(iter(table.items.values()))
    assert item["jobType"] == "PRICE_LIST"
    assert item["status"] == "QUEUED"
    assert item["expiresAt"] > item["createdAt"]


def test_disallowed_file_type_is_rejected_before_upload(api_env):
    _t, lam, s3 = api_env
    response = api.handler(post_price_list(content_type="application/pdf"), None)
    assert response["statusCode"] == 400
    assert s3.objects == [] and lam.invocations == []


def test_oversized_image_is_rejected(api_env):
    _t, lam, s3 = api_env
    huge = b"\x89PNG\r\n\x1a\n" + b"x" * (api.MAX_IMAGE_BYTES + 1)
    response = api.handler(post_price_list(image=huge), None)
    assert response["statusCode"] == 413
    assert s3.objects == [] and lam.invocations == []


def test_oversized_upload_body_is_rejected(api_env):
    _t, lam, s3 = api_env
    event = {"routeKey": "POST /api/supplier-price-lists",
             "body": "x" * (api.MAX_UPLOAD_BODY_BYTES + 1)}
    assert api.handler(event, None)["statusCode"] == 413
    assert s3.objects == [] and lam.invocations == []


def test_invalid_base64_is_rejected(api_env):
    _t, _lam, s3 = api_env
    response = api.handler(post_price_list(encoded="not base64!!"), None)
    assert response["statusCode"] == 400
    assert s3.objects == []


def test_missing_image_is_rejected(api_env):
    event = {"routeKey": "POST /api/supplier-price-lists",
             "body": json.dumps({"contentType": "image/png"})}
    assert api.handler(event, None)["statusCode"] == 400


# ---- owner decisions on a detected change ----

REVIEW_RESULT = {
    "status": "REVIEWED",
    "jobType": "PRICE_LIST",
    "review": {"lines": [{
        "status": "MATCHED",
        "skuId": "W-FIN-1.5-RED-90M",
        "comparison": {"previousPrice": 5900.0, "currentPrice": 6300.0,
                       "percentageDelta": 6.78, "materialChange": True},
    }]},
}


def seed_reviewed_job(table) -> str:
    job_id = "a" * 32
    table.put_item(Item={
        "PK": f"JOB#{job_id}", "SK": "META", "jobId": job_id,
        "jobType": "PRICE_LIST", "status": "DONE", "createdAt": 1,
        "result": json.dumps(REVIEW_RESULT),
    })
    return job_id


def post_decision(job_id, sku_id, decision):
    return {"routeKey": "POST /api/price-decisions",
            "body": json.dumps({"jobId": job_id, "skuId": sku_id,
                                "decision": decision})}


def test_owner_can_confirm_a_price_change(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    response = api.handler(
        post_decision(job_id, "W-FIN-1.5-RED-90M", "CONFIRMED"), None)

    assert response["statusCode"] == 201
    record = body_of(response)
    assert record["decision"] == "CONFIRMED"
    assert record["percentageDelta"] == 6.78
    assert record["catalogPriceChanged"] is False


def test_owner_can_reject_a_price_change(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    response = api.handler(
        post_decision(job_id, "W-FIN-1.5-RED-90M", "REJECTED"), None)
    assert body_of(response)["decision"] == "REJECTED"


def test_a_decision_is_returned_with_the_job(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, "W-FIN-1.5-RED-90M", "CONFIRMED"), None)

    job = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}",
         "pathParameters": {"jobId": job_id}}, None))
    assert len(job["decisions"]) == 1
    assert job["decisions"][0]["skuId"] == "W-FIN-1.5-RED-90M"


def test_decision_figures_come_from_the_stored_job_not_the_request(api_env):
    """A caller cannot post a percentage the engine never calculated."""
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    event = {"routeKey": "POST /api/price-decisions", "body": json.dumps({
        "jobId": job_id, "skuId": "W-FIN-1.5-RED-90M", "decision": "CONFIRMED",
        "percentageDelta": 99.9, "previousPrice": 1.0, "currentPrice": 2.0,
    })}
    record = body_of(api.handler(event, None))
    assert record["percentageDelta"] == 6.78
    assert record["previousPrice"] == 5900.0


def test_decision_on_an_invented_sku_is_rejected(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    response = api.handler(post_decision(job_id, "MADE-UP-SKU", "CONFIRMED"), None)
    assert response["statusCode"] == 400


def test_decision_on_a_sku_with_no_change_on_this_list_is_rejected(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    response = api.handler(post_decision(job_id, "SW-ANC-1W10A", "CONFIRMED"), None)
    assert response["statusCode"] == 400


def test_invalid_decision_value_is_rejected(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    response = api.handler(post_decision(job_id, "W-FIN-1.5-RED-90M", "MAYBE"), None)
    assert response["statusCode"] == 400


def test_decision_on_an_unknown_job_is_a_404(api_env):
    response = api.handler(post_decision("b" * 32, "W-FIN-1.5-RED-90M", "CONFIRMED"), None)
    assert response["statusCode"] == 404


def test_decision_prices_are_written_as_decimal_not_float(api_env):
    """DynamoDB rejects Python floats outright, which surfaced in production
    as a 500 on the very first confirmation."""
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, "W-FIN-1.5-RED-90M", "CONFIRMED"), None)

    stored = table.items[(f"DECISION#{job_id}", "SKU#W-FIN-1.5-RED-90M")]
    for field in ("previousPrice", "currentPrice", "percentageDelta"):
        assert isinstance(stored[field], Decimal), f"{field} stored as float"
    # And the exact value survives the conversion.
    assert stored["currentPrice"] == Decimal("6300.0")


def test_stored_decimals_are_returned_to_the_browser_as_numbers(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, "W-FIN-1.5-RED-90M", "CONFIRMED"), None)

    job = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}",
         "pathParameters": {"jobId": job_id}}, None))
    assert job["decisions"][0]["currentPrice"] == 6300.0
