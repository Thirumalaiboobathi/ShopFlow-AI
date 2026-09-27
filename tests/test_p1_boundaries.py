"""P1 hardening: the customer boundary, reproduced attack by attack.

A second independent evaluation found three things on the public API:

  P1-A  `POST /api/whatsapp/send` with CREDIT_STATUS or CREDIT_REMINDER, and
        no owner marker, returned a khata customer's name, outstanding
        balance, credit limit, hold status - and their full phone number
        inside `draftUrl`, beside a `recipient` field that was masked.
  P1-B  `GET /api/jobs/{id}` for an order returned `onHand`,
        `weeklyVelocity`, `coverageWeeks`, `modelId`, `turns`, and the
        model's raw bad tool arguments inside `lineIsolation`.
  P1-C  the owner routes are behind a demo header, which is not
        authentication - and must never be described as if it were.

Each is reproduced here exactly, and each assertion walks the WHOLE response
at every depth rather than checking a few known names.
"""

from __future__ import annotations

import json
import re

import pytest

import lambdas.api.handler as api
from agent import decision_trace
from engine.loader import cached_dataset
from engine.quote import calculate_quote
from integrations import whatsapp
from test_api import FakeTable, body_of
from test_whatsapp_api import enable, fake_urlopen

OWNER = {"x-shopflow-demo-owner": "demo-workspace"}
KUMAR = "CUST-KUMAR-004"      # the account the evaluator read: BLOCKED
RAVI = "CUST-RAVI-001"
JOB = "e" * 32

CANONICAL = [{"skuId": "SW-ANC-1W10A", "quantity": 20},
             {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
             {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}]


def walk(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key, child
            yield from walk(child, here)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, f"{path}[]")


def digits(text) -> str:
    return re.sub(r"\D", "", str(text))


@pytest.fixture
def env(monkeypatch):
    table = FakeTable()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setattr(api, "table", lambda: table)
    for name in (whatsapp.ENV_ENABLED, whatsapp.ENV_PHONE_ID, whatsapp.ENV_TOKEN):
        monkeypatch.delenv(name, raising=False)
    return table


def seed_order(table, customer_id=RAVI):
    quote = calculate_quote(cached_dataset(), CANONICAL).as_dict()
    table.put_item({"PK": f"JOB#{JOB}", "SK": "META", "jobId": JOB,
                    "jobType": "ORDER", "status": "DONE",
                    "customerId": customer_id,
                    "result": json.dumps({"status": "QUOTED", "quote": quote,
                                          "credit": {"decision": "APPROVED",
                                                     "creditLimit": 15000.0}})})
    return quote


def send(body, owner=False):
    event = {"routeKey": "POST /api/whatsapp/send", "body": json.dumps(body)}
    if owner:
        event["headers"] = dict(OWNER)
    return api.handler(event, None)


# ---------------------------------------------------------------------------
# P1-A  WhatsApp: the evaluator's attack, exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message_type", ["CREDIT_STATUS", "CREDIT_REMINDER"])
@pytest.mark.parametrize("customer", [KUMAR, RAVI])
def test_p1a_anonymous_khata_message_is_refused(env, message_type, customer):
    response = send({"messageType": message_type, "customerId": customer,
                     "orderTotal": 5000})
    assert response["statusCode"] == 401
    body = body_of(response)
    assert body["demoGate"] is True
    assert body["isAuthentication"] is False

    account = cached_dataset().customer(customer)
    served = response["body"]
    assert account.customerName not in served
    assert digits(account.phone)[-10:] not in digits(served)
    for figure in (account.creditLimit, account.outstandingAmount):
        assert f"{figure:,.2f}" not in served and str(figure) not in served
    for word in ("Outstanding", "Credit limit", "On hold", "draftUrl",
                 "recipient"):
        assert word not in served


@pytest.mark.parametrize("message_type", ["QUOTATION", "ORDER_CONFIRMATION"])
def test_p1a_an_anonymous_quotation_draft_carries_no_customer(env,
                                                              message_type):
    seed_order(env)
    response = send({"messageType": message_type, "quoteId": JOB,
                     "customerId": RAVI})
    assert response["statusCode"] == 200
    body = body_of(response)
    assert set(body) <= api.PUBLIC_WHATSAPP_FIELDS
    assert body["status"] == "DRAFT" and body["sent"] is False
    assert body["reason"] == api.PUBLIC_DRAFT_REASON
    # The draft link opens WhatsApp without a number: the shop picks the
    # contact. No phone number, masked or full, is anywhere in the response.
    assert body["draftUrl"].startswith("https://wa.me/?text=")
    account = cached_dataset().customer(RAVI)
    assert digits(account.phone)[-10:] not in digits(response["body"])
    assert account.customerName not in response["body"]
    assert "Credit status" not in body["text"]
    # The quotation itself is still there, with its real total.
    assert "22,306.48" in body["text"]


def test_p1a_an_anonymous_caller_can_never_trigger_a_send(env, monkeypatch):
    seed_order(env)
    enable(monkeypatch)
    captured = []
    fake_urlopen(monkeypatch, capture=captured)
    body = body_of(send({"messageType": "QUOTATION", "quoteId": JOB}))
    assert body["status"] == "DRAFT" and body["sent"] is False
    assert captured == []


def test_p1a_no_phone_digits_survive_anywhere_in_any_anonymous_response(env):
    seed_order(env)
    phones = {digits(c.phone)[-10:] for c in cached_dataset().customers.values()}
    requests = [
        {"messageType": t, "quoteId": JOB, "customerId": c}
        for t in ("QUOTATION", "ORDER_CONFIRMATION", "CREDIT_STATUS",
                  "CREDIT_REMINDER")
        for c in cached_dataset().customers
    ]
    for request in requests:
        served = digits(send(request)["body"])
        for phone in phones:
            assert phone not in served, request


def test_p1a_the_owner_still_gets_the_whole_khata_message(env):
    body = body_of(send({"messageType": "CREDIT_STATUS", "customerId": KUMAR},
                        owner=True))
    assert body["messageType"] == "CREDIT_STATUS"
    assert "Outstanding" in body["text"] and "21,750.00" in body["text"]
    # Masked in `recipient`; the number the draft needs stays behind the gate.
    assert "*" in body["recipient"]
    assert body["draftUrl"].startswith("https://wa.me/91")


# ---------------------------------------------------------------------------
# P1-B  the customer's job view is default-deny at every depth
# ---------------------------------------------------------------------------

INTERNAL_KEYS = frozenset({
    "onHand", "weeklyVelocity", "coverageWeeks", "shortageQty", "modelId",
    "turns", "elapsedMs", "matches", "lineIsolation", "changes", "trace",
    "candidates", "diagnostic", "instruction", "blocking", "proposedQuantity",
    "proposed", "customerId", "customerName", "credit", "marginProtection",
    "supplierCost", "unitCost", "costPrice", "suggestedSellingPrice",
    "previousSupplierCost", "confirmedSupplierCost", "toolUseId", "input",
    "errorKind", "error",
})


def stored_order_result():
    """The shape the worker actually stores, including everything internal."""
    quote = calculate_quote(cached_dataset(), CANONICAL).as_dict()
    return {
        "status": "QUOTED", "summary": "3 items quoted at Rs 22306.48.",
        "message": "", "quote": quote, "clarification": None,
        "grounded": True, "ungroundedNumbers": [],
        "modelId": "apac.amazon.nova-pro-v1:0", "turns": 5, "elapsedMs": 5943.3,
        "matches": [{"status": "RESOLVED", "skuId": "SW-ANC-1W10A",
                     "requestedText": "20 Anchor modular switches",
                     "lineIsolation": {"changes": [
                         {"filter": "brand", "from": "Havells", "to": "Anchor"},
                         {"filter": "colour", "from": "red", "to": "White"}]},
                     "instruction": "Use this skuId."}],
        "trace": [{"turn": 1, "tool": "calculate_quote", "ok": False,
                   "input": {"items": [{"skuId": "MCB-HAV-SP-32A-C",
                                        "quantity": 4}]},
                   "error": "quantity mismatch", "errorKind": "INVALID_QUANTITY"}],
        "marginProtection": {"affected": [{"confirmedSupplierCost": 6300.0}]},
        "credit": {"creditLimit": 15000.0, "outstandingAmount": 8500.0},
        "decisionTrace": decision_trace.build(
            {"status": "QUOTED", "quote": quote, "matches": [
                {"status": "RESOLVED", "skuId": "SW-ANC-1W10A",
                 "requestedText": "model wording",
                 "lineIsolation": {"changes": [{"filter": "brand",
                                                "reason": "the line names Anchor"}]}}]},
            quantity_check=[{"skuId": "MCB-HAV-SP-32A-C", "customerQuantity": 2,
                             "proposedQuantity": 4, "quotedQuantity": 2,
                             "verdict": "VERIFIED"}]),
        "futureInternalField": {"secret": "should never be public"},
    }


def job_body(result):
    return {"jobId": JOB, "jobType": "ORDER", "status": "DONE",
            "orderText": "20 Anchor switches", "customerId": RAVI,
            "language": "en", "createdAt": 1, "result": result,
            "futureEnvelopeField": {"x": 1}}


def test_p1b_no_internal_key_survives_at_any_depth():
    view = api.customer_job_view(job_body(stored_order_result()))
    found = [path for path, key, _ in walk(view) if key in INTERNAL_KEYS]
    assert found == []


def test_p1b_a_field_added_tomorrow_is_absent_until_allowed():
    view = api.customer_job_view(job_body(stored_order_result()))
    served = json.dumps(view)
    assert "futureInternalField" not in served
    assert "futureEnvelopeField" not in served
    assert "should never be public" not in served


def test_p1b_the_model_raw_tool_arguments_are_gone():
    served = json.dumps(api.customer_job_view(job_body(stored_order_result())))
    for raw in ('"from": "Havells"', '"from": "red"', "model wording",
                "the line names Anchor", "quantity mismatch", '"quantity": 4'):
        assert raw not in served, raw


def test_p1b_an_allowed_field_cannot_smuggle_a_nested_object():
    result = stored_order_result()
    result["quote"]["lines"][0]["name"] = {"onHand": 14, "cost": 58.0}
    result["quote"]["total"] = [6300.0]
    view = api.customer_job_view(job_body(result))
    line = view["result"]["quote"]["lines"][0]
    assert "name" not in line
    assert "total" not in view["result"]["quote"]


def test_p1b_the_stock_evidence_string_is_gone():
    """"ordered 20, 14 in stock, short 6" was an allowed evidence string."""
    served = json.dumps(api.customer_job_view(job_body(stored_order_result())))
    assert "in stock, short" not in served
    assert "weeks of cover" not in served


def test_p1b_the_customer_still_receives_the_whole_quotation():
    view = api.customer_job_view(job_body(stored_order_result()))
    quote = view["result"]["quote"]
    assert quote["total"] == 22306.48
    assert [(l["skuId"], l["quantity"], l["sellingPrice"], l["lineTotal"],
             l["inStock"]) for l in quote["lines"]] == [
        ("SW-ANC-1W10A", 20, 78.3, 1566.0, False),
        ("W-FIN-1.5-RED-90M", 3, 6608.0, 19824.0, False),
        ("MCB-HAV-SP-32A-C", 2, 458.24, 916.48, True)]
    assert view["result"]["decisionTrace"]["available"] is True


def test_p1b_the_public_trace_has_availability_not_stock_counts():
    view = api.customer_job_view(job_body(stored_order_result()))
    trace = view["result"]["decisionTrace"]
    steps = {s["step"] for s in trace["steps"]}
    assert "AVAILABILITY" in steps
    assert not steps & {"INVENTORY_CHECK", "SHORTAGE_DETECTED", "LINE_ISOLATED",
                        "CATALOGUE_SEARCH", "PROPOSAL_REJECTED"}
    for step in trace["steps"]:
        assert set(step) <= decision_trace.PUBLIC_FIELDS
    rendered = " ".join(trace["lines"])
    assert "model proposed" not in rendered
    assert not re.search(r"\d+ in stock", rendered)


def test_p1b_the_owner_view_is_unchanged(env):
    env.put_item({"PK": f"JOB#{JOB}", "SK": "META", "jobId": JOB,
                  "jobType": "ORDER", "status": "DONE", "createdAt": 1,
                  "result": json.dumps(stored_order_result())})
    owner = body_of(api.handler({"routeKey": "GET /api/jobs/{jobId}",
                                 "headers": dict(OWNER),
                                 "pathParameters": {"jobId": JOB}}, None))
    assert owner["result"]["quote"]["lines"][0]["onHand"] == 14
    assert owner["result"]["modelId"] == "apac.amazon.nova-pro-v1:0"
    assert owner["result"]["matches"][0]["lineIsolation"]["changes"]
    assert owner["result"]["marginProtection"]


def test_p1b_the_real_route_serves_the_public_view(env):
    env.put_item({"PK": f"JOB#{JOB}", "SK": "META", "jobId": JOB,
                  "jobType": "ORDER", "status": "DONE", "createdAt": 1,
                  "customerId": RAVI,
                  "result": json.dumps(stored_order_result())})
    response = api.handler({"routeKey": "GET /api/jobs/{jobId}",
                            "pathParameters": {"jobId": JOB}}, None)
    assert response["headers"]["x-shopflow-audience"] == "customer"
    body = body_of(response)
    assert [p for p, k, _ in walk(body) if k in INTERNAL_KEYS] == []


def test_p1b_a_failed_run_shows_no_model_prose():
    result = dict(stored_order_result(), status="FAILED", quote=None,
                  summary="<thinking>I will try search_catalog</thinking> hmm",
                  message="The agent repeatedly produced invalid tool arguments")
    view = api.customer_job_view(job_body(result))
    assert view["result"]["summary"] == api.CUSTOMER_FAILURE_MESSAGE
    assert view["result"]["message"] == api.CUSTOMER_FAILURE_MESSAGE
    assert "search_catalog" not in json.dumps(view)


# ---------------------------------------------------------------------------
# P1-C  the owner gate: honest about what it is, and default-deny
# ---------------------------------------------------------------------------

def test_p1c_every_route_is_classified_exactly_once():
    classes = [api.OWNER_ROUTES, api.CUSTOMER_FACING_ROUTES, api.PUBLIC_ROUTES]
    for route in api.ROUTES:
        assert sum(route in c for c in classes) == 1, route


def test_p1c_an_unclassified_route_fails_closed(monkeypatch):
    monkeypatch.setitem(api.ROUTES, "GET /api/secret-new-thing",
                        lambda event: api._response(200, {"secret": True}))
    response = api.handler({"routeKey": "GET /api/secret-new-thing"}, None)
    assert response["statusCode"] == 404
    assert "secret" not in response["body"]


@pytest.mark.parametrize("route", sorted(api.OWNER_ROUTES))
def test_p1c_every_owner_route_refuses_a_customer(route):
    response = api.handler({"routeKey": route, "body": "{}"}, None)
    assert response["statusCode"] == 401
    body = body_of(response)
    assert body["isAuthentication"] is False
    assert "not authentication" in body["note"].lower()


def test_p1c_the_gate_says_what_it_is_in_the_code():
    assert "not authentication" in api.OWNER_GATE_NOTE.lower()


def test_p1c_what_if_travels_on_an_owner_route():
    assert "POST /api/shop-queries" in api.OWNER_ROUTES
    response = api.handler({"routeKey": "POST /api/shop-queries",
                            "body": json.dumps({"kind": "WHAT_IF",
                                                "question": "What if cost rises 5%?"})},
                           None)
    assert response["statusCode"] == 401
