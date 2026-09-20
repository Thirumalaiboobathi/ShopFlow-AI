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
        """Interpret the condition properly, including `pk AND begins_with(sk)`.

        An earlier version read `_values[1]` and assumed it was the partition
        key string. That silently returned nothing for a composite condition -
        the planner would have found no confirmed costs and every test would
        still have passed. The fake now parses what it is actually given.
        """
        pk, prefix = self._parse(KeyConditionExpression)
        if pk is None:
            raise AssertionError(
                "FakeTable.query could not find a partition key in the condition")
        return {"Items": [
            dict(v) for k, v in self.items.items()
            if k[0] == pk and (prefix is None or k[1].startswith(prefix))
        ]}

    @staticmethod
    def _parse(condition):
        """Pull the PK equality and any SK begins_with out of the condition."""
        from boto3.dynamodb.conditions import And, BeginsWith, Equals

        pk = prefix = None
        stack = [condition]
        while stack:
            node = stack.pop()
            if isinstance(node, And):
                stack.extend(node._values)
            elif isinstance(node, Equals):
                name, value = node._values
                if name.name == "PK":
                    pk = value
            elif isinstance(node, BeginsWith):
                name, value = node._values
                if name.name == "SK":
                    prefix = value
        return pk, prefix


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
    "review": {"documentDate": "15-09-2026", "lines": [{
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


# ---- purchase plans (Stage 5) ----

def post_plan(body: dict):
    return {"routeKey": "POST /api/purchase-plans", "body": json.dumps(body)}


def test_purchase_plan_is_answered_synchronously(api_env):
    """No job, no polling. The planner calls no model, so there is nothing
    to wait for - see the handler module docstring."""
    _table, lam, _s3 = api_env
    response = api.handler(post_plan({"budget": 25000}), None)

    assert response["statusCode"] == 200
    assert lam.invocations == []  # no worker was queued

    plan = body_of(response)
    assert plan["budget"] == 25000.0
    assert plan["totalSpend"] <= 25000.0
    assert plan["remaining"] >= 0


def test_purchase_plan_never_exceeds_the_budget(api_env):
    for budget in (0, 1000, 20000, 25000, 30000):
        plan = body_of(api.handler(post_plan({"budget": budget}), None))
        assert plan["totalSpend"] <= budget
        assert plan["remaining"] >= 0


def test_purchase_plan_rejects_a_negative_budget(api_env):
    response = api.handler(post_plan({"budget": -100}), None)
    assert response["statusCode"] == 400
    assert "negative" in body_of(response)["error"]


@pytest.mark.parametrize("bad", [{"budget": "lots"}, {"budget": None}, {}])
def test_purchase_plan_rejects_a_non_numeric_budget(api_env, bad):
    assert api.handler(post_plan(bad), None)["statusCode"] == 400


def test_purchase_plan_rejects_an_absurd_budget(api_env):
    response = api.handler(post_plan({"budget": 10 ** 12}), None)
    assert response["statusCode"] == 400


def test_purchase_plan_carries_the_two_what_if_budgets(api_env):
    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    assert [s["budget"] for s in plan["whatIf"]] == [20000.0, 30000.0]
    for scenario in plan["whatIf"]:
        assert scenario["totalSpend"] <= scenario["budget"]


def test_purchase_plan_uses_a_confirmed_price_from_the_stored_decision(api_env):
    """The confirmed cost is read from DynamoDB, never from the request."""
    table, _lam, _s3 = api_env
    job_id = "a" * 32
    table.put_item(Item={
        "PK": f"DECISION#{job_id}",
        "SK": "SKU#W-FIN-1.5-RED-90M",
        "skuId": "W-FIN-1.5-RED-90M",
        "decision": "CONFIRMED",
        # Stored as Decimal, exactly as the decision route writes it.
        "previousPrice": Decimal("5900.0"),
        "currentPrice": Decimal("6300.0"),
    })

    baseline = body_of(api.handler(post_plan({"budget": 25000}), None))
    repriced = body_of(api.handler(
        post_plan({"budget": 25000, "priceListJobId": job_id}), None))

    wire = next(l for l in repriced["commitments"]
                if l["skuId"] == "W-FIN-1.5-RED-90M")
    assert wire["unitCost"] == 6300.0
    # Two coils short, so the confirmed rise costs exactly 800 more.
    assert repriced["commitmentCost"] - baseline["commitmentCost"] == 800.0
    assert repriced["totalSpend"] <= 25000.0
    assert repriced["remaining"] >= 0


def test_purchase_plan_ignores_a_rejected_price_decision(api_env):
    table, _lam, _s3 = api_env
    job_id = "b" * 32
    table.put_item(Item={
        "PK": f"DECISION#{job_id}",
        "SK": "SKU#W-FIN-1.5-RED-90M",
        "skuId": "W-FIN-1.5-RED-90M",
        "decision": "REJECTED",
        "previousPrice": Decimal("5900.0"),
        "currentPrice": Decimal("6300.0"),
    })
    plan = body_of(api.handler(
        post_plan({"budget": 25000, "priceListJobId": job_id}), None))

    wire = next(l for l in plan["commitments"] if l["skuId"] == "W-FIN-1.5-RED-90M")
    assert wire["unitCost"] == 5900.0
    assert plan["confirmedCosts"] == []


def test_purchase_plan_rejects_a_malformed_price_list_job_id(api_env):
    response = api.handler(
        post_plan({"budget": 25000, "priceListJobId": "not-a-job"}), None)
    assert response["statusCode"] == 400


def test_purchase_plan_is_json_serialisable_end_to_end(api_env):
    """Every figure must survive json.dumps - no Decimal, no inf.

    Coverage is math.inf for a dead-stock SKU inside the engine, and inf is
    not valid JSON. It must never reach the response body.
    """
    response = api.handler(post_plan({"budget": 25000}), None)
    assert "Infinity" not in response["body"]
    assert "NaN" not in response["body"]
    json.loads(response["body"])


# ---- Stage 5.1: durable confirmed supplier purchase costs ----

WIRE = "W-FIN-1.5-RED-90M"
COST_PK = "SHOP#demo"
COST_SK = f"COST#{WIRE}"


def cost_row(table):
    return table.items.get((COST_PK, COST_SK))


def test_1_confirming_a_change_persists_the_purchase_cost(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)

    response = api.handler(post_decision(job_id, WIRE, "CONFIRMED"), None)
    assert response["statusCode"] == 201
    assert body_of(response)["purchaseCostPersisted"] is True

    row = cost_row(table)
    assert row is not None
    assert row["skuId"] == WIRE
    assert row["confirmedCost"] == Decimal("6300.0")
    assert row["currency"] == "INR"
    assert row["sourceJobId"] == job_id
    assert row["effectiveDate"] == "15-09-2026"
    assert row["supplierId"]
    assert int(row["confirmedAt"]) > 0


def test_1b_the_persisted_cost_is_decimal_not_float(api_env):
    """The Stage 4 bug, guarded at the new write site.

    DynamoDB rejects Python floats and a cost record carries one. The fake
    store would accept either, so this asserts the type the real table needs.
    """
    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)
    assert isinstance(cost_row(table)["confirmedCost"], Decimal)


def test_1c_the_cost_record_has_no_ttl(api_env):
    """Job records expire. The shop's purchase cost must not."""
    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)
    assert "expiresAt" not in cost_row(table)


def test_2_the_confirmed_cost_survives_a_reload(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, WIRE, "CONFIRMED"), None)

    job = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "pathParameters": {"jobId": job_id}},
        None))
    assert [d["skuId"] for d in job["decisions"]] == [WIRE]
    assert cost_row(table)["confirmedCost"] == Decimal("6300.0")


def test_3_planner_without_a_job_id_uses_the_confirmed_cost(api_env):
    """The point of Stage 5.1: no document needed, just a budget."""
    table, _lam, _s3 = api_env

    before = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire_before = next(l for l in before["commitments"] if l["skuId"] == WIRE)
    assert wire_before["unitCost"] == 5900.0
    assert wire_before["costSource"] == "SEEDED_SUPPLIER_PRICE"

    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)

    after = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire_after = next(l for l in after["commitments"] if l["skuId"] == WIRE)
    assert wire_after["unitCost"] == 6300.0
    assert wire_after["costSource"] == "CONFIRMED_SUPPLIER_PRICE"
    assert after["commitmentCost"] - before["commitmentCost"] == 800.0
    assert after["totalSpend"] <= 25000.0
    assert after["remaining"] >= 0


def test_3b_the_plan_reports_where_the_confirmed_cost_came_from(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, WIRE, "CONFIRMED"), None)

    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)

    assert wire["costProvenance"]["source"] == "CONFIRMED_SUPPLIER_PRICE"
    assert wire["costProvenance"]["sourceJobId"] == job_id
    assert wire["costProvenance"]["confirmedAt"]
    assert plan["confirmedCosts"][0]["sourceJobId"] == job_id


def test_3c_unconfirmed_skus_are_labelled_seeded(api_env):
    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)

    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    others = [l for l in plan["commitments"] + plan["restockSelected"]
              if l["skuId"] != WIRE]
    assert others
    for line in others:
        assert line["costSource"] == "SEEDED_SUPPLIER_PRICE"


def test_4_confirmed_cost_does_not_change_the_selling_price(api_env):
    table, _lam, _s3 = api_env
    before = body_of(api.handler(post_plan({"budget": 25000}), None))
    selling = next(l for l in before["commitments"]
                   if l["skuId"] == WIRE)["sellingPrice"]

    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)

    after = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire = next(l for l in after["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == 6300.0
    assert wire["sellingPrice"] == selling == 6608.0


def test_5_rejecting_a_change_persists_no_purchase_cost(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)

    response = api.handler(post_decision(job_id, WIRE, "REJECTED"), None)
    assert response["statusCode"] == 201
    assert body_of(response)["purchaseCostPersisted"] is False
    assert cost_row(table) is None

    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == 5900.0
    assert wire["costSource"] == "SEEDED_SUPPLIER_PRICE"


def test_6_a_newer_confirmation_supersedes_the_older_one(api_env):
    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)
    assert cost_row(table)["confirmedCost"] == Decimal("6300.0")
    first_job = cost_row(table)["sourceJobId"]

    # A later price list for the same SKU, at a different rate.
    later_job = "c" * 32
    table.put_item(Item={
        "PK": f"JOB#{later_job}", "SK": "META", "jobId": later_job,
        "jobType": "PRICE_LIST", "status": "DONE", "createdAt": 2,
        "result": json.dumps({
            "status": "REVIEWED", "jobType": "PRICE_LIST",
            "review": {"documentDate": "01-10-2026", "lines": [{
                "status": "MATCHED", "skuId": WIRE,
                "comparison": {"previousPrice": 6300.0, "currentPrice": 6750.0,
                               "percentageDelta": 7.14, "materialChange": True},
            }]},
        }),
    })
    api.handler(post_decision(later_job, WIRE, "CONFIRMED"), None)

    row = cost_row(table)
    assert row["confirmedCost"] == Decimal("6750.0")
    assert row["sourceJobId"] == later_job != first_job
    assert row["effectiveDate"] == "01-10-2026"

    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == 6750.0


def test_7_the_source_job_id_survives_into_the_plan(api_env):
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, WIRE, "CONFIRMED"), None)

    assert cost_row(table)["sourceJobId"] == job_id
    plan = body_of(api.handler(post_plan({"budget": 25000}), None))
    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["costProvenance"]["sourceJobId"] == job_id


def test_the_explicit_job_id_path_still_works(api_env):
    """Backward compatibility with the Stage 5 caller."""
    table, _lam, _s3 = api_env
    job_id = seed_reviewed_job(table)
    api.handler(post_decision(job_id, WIRE, "CONFIRMED"), None)

    plan = body_of(api.handler(
        post_plan({"budget": 25000, "priceListJobId": job_id}), None))
    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == 6300.0
    assert wire["costSource"] == "CONFIRMED_SUPPLIER_PRICE"


def test_a_confirmation_writes_exactly_one_cost_row(api_env):
    """One current cost per SKU - the store must not accumulate history."""
    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)

    cost_rows = [k for k in table.items if k[0] == COST_PK]
    assert cost_rows == [(COST_PK, COST_SK)]


def test_the_planner_query_really_reaches_the_cost_rows(api_env):
    """Guards the fake store itself.

    An earlier FakeTable.query read `_values[1]` and assumed a bare partition
    key, so a composite `PK = x AND begins_with(SK, ...)` silently matched
    nothing. Every planner test still passed while the confirmed cost never
    arrived. This asserts the query returns the row that was written.
    """
    from boto3.dynamodb.conditions import Key

    table, _lam, _s3 = api_env
    api.handler(post_decision(seed_reviewed_job(table), WIRE, "CONFIRMED"), None)

    found = table.query(
        KeyConditionExpression=Key("PK").eq(COST_PK) & Key("SK").begins_with("COST#")
    )["Items"]
    assert [r["skuId"] for r in found] == [WIRE]

    # And the prefix is honoured rather than ignored.
    none = table.query(
        KeyConditionExpression=Key("PK").eq(COST_PK) & Key("SK").begins_with("NOPE#")
    )["Items"]
    assert none == []
