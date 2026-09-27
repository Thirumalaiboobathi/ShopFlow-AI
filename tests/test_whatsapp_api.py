"""WhatsApp Business Cloud API: the adapter, the messages, and the boundary.

Most of this file is about what a customer must NOT receive and what a send
must NOT change.

A quotation line carries the shop's own working - what it pays its supplier,
how fast the item moves, what margin the line earns. A message that leaked any
of it would damage the relationship the product exists to serve, so the tests
below take real engine output, render it, and assert the shop's numbers are
absent.

The other half is that sending is a message and nothing more. It prices
nothing, moves no balance, touches no stock and writes no row. A successful
send is not an accounting transaction, and these tests hold the table and the
catalogue to that.

No credentials exist in this repository, so the Cloud API call itself is
mocked at the HTTP boundary. What is verified is the request the adapter
builds, every error it maps, and that a token never leaves it.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import lambdas.api.handler as api
from engine.credit import check_quote_credit
from engine.loader import cached_dataset
from engine.messages import (
    CREDIT_REMINDER,
    CREDIT_STATUS,
    INTERNAL_ONLY_FIELDS,
    ORDER_CONFIRMATION,
    QUOTATION,
    InvalidMessageRequest,
    build_credit_status_message,
    build_order_confirmation_message,
    build_quotation_message,
    customer_safe_quote,
    format_units,
    mask_phone,
    normalize_phone,
)
from engine.quote import calculate_quote
from integrations import whatsapp
from test_api import FakeQueue, FakeTable

RAVI = "CUST-RAVI-001"
WIRE = "W-FIN-1.5-RED-90M"
CANONICAL = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": WIRE, "quantity": 3, "uom": "COIL"},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
TOKEN = "EAAtest-not-a-real-token-0000"


@pytest.fixture(scope="module")
def quote():
    return calculate_quote(cached_dataset(), CANONICAL).as_dict()


@pytest.fixture(scope="module")
def credit(quote):
    return check_quote_credit(cached_dataset(), RAVI, quote)


@pytest.fixture(autouse=True)
def disabled_by_default(monkeypatch):
    """Every test starts with WhatsApp off, which is the shipped state."""
    for name in (whatsapp.ENV_ENABLED, whatsapp.ENV_PHONE_ID,
                 whatsapp.ENV_TOKEN, whatsapp.ENV_TOKEN_SECRET,
                 whatsapp.ENV_TEMPLATE, whatsapp.ENV_TEMPLATE_LANG,
                 whatsapp.ENV_API_VERSION):
        monkeypatch.delenv(name, raising=False)


def enable(monkeypatch, **extra):
    monkeypatch.setenv(whatsapp.ENV_ENABLED, "true")
    monkeypatch.setenv(whatsapp.ENV_PHONE_ID, "1234567890")
    monkeypatch.setenv(whatsapp.ENV_TOKEN, TOKEN)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(monkeypatch, payload=None, status=200, error=None, capture=None):
    def opener(request, timeout=None):
        if capture is not None:
            capture.append({
                "url": request.full_url,
                "headers": dict(request.headers),
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            })
        if error is not None:
            raise error
        return FakeResponse(payload or {"messages": [{"id": "wamid.TEST"}]}, status)

    monkeypatch.setattr(whatsapp.urllib.request, "urlopen", opener)


# ===========================================================================
# 1-5  the messages
# ===========================================================================

def test_1_a_quotation_message_carries_the_engines_own_total(quote, credit):
    message = build_quotation_message(
        quote, {"customerName": "Ravi Electrical Works"}, credit)

    assert message["messageType"] == QUOTATION
    text = message["text"]
    assert "ShopFlow AI — Quotation" in text
    assert "Ravi Electrical Works" in text
    assert "Total: ₹22,306.48" in text
    for line in quote["lines"]:
        assert line["name"] in text
        assert f"{line['lineTotal']:,.2f}" in text


def test_2_the_unit_is_preserved_and_never_converted(quote):
    """"3 COIL" on the quotation becomes "3 coils" - not 270 metres."""
    message = build_quotation_message(quote)
    assert "3 coils" in message["text"]
    assert "20 pieces" in message["text"]
    # The equivalence is internal presentation and has no business here.
    assert "270" not in message["text"]
    assert "METER" not in message["text"].upper()


def test_2b_units_are_pluralised_from_the_quantity_alone():
    assert format_units(1, "COIL") == "1 coil"
    assert format_units(3, "COIL") == "3 coils"
    assert format_units(0, "PIECE") == "0 pieces"


def test_3_no_internal_figure_reaches_a_customer(quote, credit):
    """The property this whole module exists for."""
    data = cached_dataset()
    text = build_quotation_message(quote, None, credit)["text"]

    # Supplier cost, catalogue cost and margin, for every line quoted.
    for line in quote["lines"]:
        product = data.product(line["skuId"])
        assert f"{product.costPrice:,.2f}" not in text
        assert f"{product.costPrice - 0:,.0f}" not in text
        margin = product.sellingPrice - product.costPrice
        assert f"{margin:,.2f}" not in text
    # Stock, cover and velocity.
    for word in ("onHand", "in stock", "shortage", "coverage", "velocity",
                 "supplier", "margin", "cost"):
        assert word.lower() not in text.lower(), word


def test_3b_the_safe_projection_is_an_allow_list(quote):
    safe = customer_safe_quote(quote)
    for line in safe["lines"]:
        assert set(line) == {"name", "quantity", "uom", "lineTotal"}
        for internal in INTERNAL_ONLY_FIELDS:
            assert internal not in line


def test_3c_a_quotation_shows_only_the_credit_outcome_not_the_balance(
        quote, credit):
    text = build_quotation_message(quote, None, credit)["text"]

    assert "Credit status:" in text
    # The customer's own balance belongs on an account message, not here.
    assert f"{credit['currentOutstanding']:,.2f}" not in text
    assert f"{credit['creditLimit']:,.2f}" not in text


def test_4_an_account_message_shows_the_customers_own_figures(credit):
    message = build_credit_status_message(
        credit, {"customerName": "Ravi Electrical Works"})

    assert message["messageType"] == CREDIT_STATUS
    text = message["text"]
    assert f"{credit['currentOutstanding']:,.2f}" in text
    assert f"{credit['creditLimit']:,.2f}" in text
    # Still nothing about the shop's side of the business.
    assert "margin" not in text.lower() and "supplier" not in text.lower()


def test_4b_a_reminder_is_worded_as_one(credit):
    message = build_credit_status_message(credit, None, reminder=True)
    assert message["messageType"] == CREDIT_REMINDER
    assert "Reminder" in message["text"]
    assert "settle" in message["text"].lower()


def test_5_an_order_confirmation_confirms_and_claims_nothing_else(quote):
    message = build_order_confirmation_message(
        quote, {"customerName": "Bala Contractors"}, reference="abc123")

    assert message["messageType"] == ORDER_CONFIRMATION
    text = message["text"]
    assert "Order Confirmation" in text
    assert "Total: ₹22,306.48" in text
    for claim in ("paid", "payment received", "dispatched", "delivered"):
        assert claim not in text.lower()


def test_5b_a_message_cannot_be_built_from_nothing():
    for bad in (None, {}, {"lines": []}):
        with pytest.raises(InvalidMessageRequest):
            build_quotation_message(bad)
    with pytest.raises(InvalidMessageRequest):
        build_credit_status_message({})


def test_5c_the_messages_module_reaches_nothing():
    import ast
    import inspect

    from engine import messages

    tree = ast.parse(inspect.getsource(messages))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"boto3", "botocore", "requests"}, imported


# ===========================================================================
# 6-8  phone numbers
# ===========================================================================

@pytest.mark.parametrize("raw,expected", [
    ("+919900000001", "+919900000001"),
    ("+91 99000 00001", "+919900000001"),
    ("+91-99000-00001", "+919900000001"),
    ("+91 (99000) 00001", "+919900000001"),
])
def test_6_valid_numbers_normalise_to_e164(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "9900000001", "+91abc0000001", "+9199", "+" + "9" * 20,
    "+91 9900\x0000001",
])
def test_6b_anything_doubtful_is_refused_rather_than_repaired(raw):
    with pytest.raises(InvalidMessageRequest):
        normalize_phone(raw)


def test_7_a_number_is_masked_for_logs_and_responses():
    assert mask_phone("+919900000001") == "+91******0001"
    assert mask_phone("+14155550123") == "+14*****0123"
    assert mask_phone("") == ""
    assert "9900" not in mask_phone("+919900000001")[3:-4]


def test_8_the_adapter_logs_a_masked_number_only(monkeypatch, capsys):
    enable(monkeypatch)
    fake_urlopen(monkeypatch)

    whatsapp.send_text("+919900000001", "hello")

    printed = capsys.readouterr().out
    assert "+919900000001" not in printed
    assert "+91******0001" in printed
    assert TOKEN not in printed


def test_8b_a_failure_logs_no_number_and_no_token(monkeypatch, capsys):
    enable(monkeypatch)
    fake_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "https://graph.facebook.com/x", 401, "Unauthorized", {},
        __import__("io").BytesIO(json.dumps(
            {"error": {"code": 190, "message": "Session expired for 12345"}}
        ).encode())))

    with pytest.raises(whatsapp.WhatsAppError):
        whatsapp.send_text("+919900000001", "hello")

    printed = capsys.readouterr().out
    assert "+919900000001" not in printed
    assert TOKEN not in printed
    # Meta's own message text can name the account, so it is not echoed.
    assert "Session expired" not in printed


# ===========================================================================
# 9-12  the adapter
# ===========================================================================

def test_9_the_safe_default_is_disabled():
    assert whatsapp.is_enabled() is False
    status = whatsapp.configuration_status()
    assert status["enabled"] is False
    with pytest.raises(whatsapp.WhatsAppError) as caught:
        whatsapp.send_text("+919900000001", "hello")
    assert caught.value.reason == whatsapp.DISABLED


def test_9b_configuration_status_reports_booleans_never_values(monkeypatch):
    enable(monkeypatch, WHATSAPP_TEMPLATE_NAME="shopflow_quote")
    status = whatsapp.configuration_status()

    assert status == {
        "enabled": True, "hasPhoneNumberId": True, "hasAccessToken": True,
        "usesSecretsManager": False, "hasTemplate": True,
        "apiVersion": whatsapp.DEFAULT_API_VERSION,
    }
    assert TOKEN not in json.dumps(status)
    assert "1234567890" not in json.dumps(status)


def test_9c_enabled_but_unconfigured_is_its_own_reason(monkeypatch):
    monkeypatch.setenv(whatsapp.ENV_ENABLED, "true")
    with pytest.raises(whatsapp.WhatsAppError) as caught:
        whatsapp.send_text("+919900000001", "hello")
    assert caught.value.reason == whatsapp.NOT_CONFIGURED


def test_10_a_send_builds_the_cloud_api_request(monkeypatch):
    enable(monkeypatch)
    captured = []
    fake_urlopen(monkeypatch, capture=captured)

    result = whatsapp.send_text("+919900000001", "Total: 100")

    assert result["sent"] is True
    assert result["messageId"] == "wamid.TEST"
    assert result["recipientMasked"] == "+91******0001"
    # The number is masked even in the adapter's own return value.
    assert "+919900000001" not in json.dumps(result)

    request = captured[0]
    assert request["url"].startswith("https://graph.facebook.com/")
    assert request["url"].endswith("/1234567890/messages")
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["body"]["messaging_product"] == "whatsapp"
    assert request["body"]["to"] == "919900000001"
    assert request["body"]["text"]["body"] == "Total: 100"
    assert request["timeout"] == whatsapp.REQUEST_TIMEOUT_SECONDS


def test_10b_a_configured_template_is_used_instead_of_free_text(monkeypatch):
    enable(monkeypatch, WHATSAPP_TEMPLATE_NAME="shopflow_quote",
           WHATSAPP_TEMPLATE_LANGUAGE="ta")
    captured = []
    fake_urlopen(monkeypatch, capture=captured)

    whatsapp.send_text("+919900000001", "Total: 100")

    body = captured[0]["body"]
    assert body["type"] == "template"
    assert body["template"]["name"] == "shopflow_quote"
    assert body["template"]["language"]["code"] == "ta"
    # The message the engine built travels as the template's parameter, so the
    # customer reads the same words either way.
    assert body["template"]["components"][0]["parameters"][0]["text"] == "Total: 100"


@pytest.mark.parametrize("status,payload,reason", [
    (401, {"error": {"code": 190}}, whatsapp.AUTH_FAILED),
    (403, {"error": {"code": 200}}, whatsapp.AUTH_FAILED),
    (429, {"error": {"code": 4}}, whatsapp.RATE_LIMITED),
    (400, {"error": {"code": 132001}}, whatsapp.TEMPLATE_REJECTED),
    (400, {"error": {"code": 131026}}, whatsapp.TEMPLATE_REJECTED),
    (400, {"error": {"code": 131030}}, whatsapp.INVALID_RECIPIENT),
    (400, {"error": {"code": 999999}}, whatsapp.API_ERROR),
    (500, {"error": {"code": 1}}, whatsapp.API_ERROR),
])
def test_11_every_api_error_maps_to_a_reason(monkeypatch, status, payload, reason):
    import io

    enable(monkeypatch)
    fake_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "https://graph.facebook.com/x", status, "err", {},
        io.BytesIO(json.dumps(payload).encode())))

    with pytest.raises(whatsapp.WhatsAppError) as caught:
        whatsapp.send_text("+919900000001", "hello")

    assert caught.value.reason == reason
    # Every reason has a sentence fit to put on a screen.
    assert caught.value.safe_message
    assert TOKEN not in caught.value.safe_message


def test_11b_a_timeout_is_its_own_reason(monkeypatch):
    import socket

    enable(monkeypatch)
    fake_urlopen(monkeypatch, error=socket.timeout())
    with pytest.raises(whatsapp.WhatsAppError) as caught:
        whatsapp.send_text("+919900000001", "hello")
    assert caught.value.reason == whatsapp.TIMEOUT


def test_11c_a_network_failure_is_its_own_reason(monkeypatch):
    enable(monkeypatch)
    fake_urlopen(monkeypatch, error=urllib.error.URLError("no route"))
    with pytest.raises(whatsapp.WhatsAppError) as caught:
        whatsapp.send_text("+919900000001", "hello")
    assert caught.value.reason == whatsapp.NETWORK_ERROR


def test_12_an_unusable_recipient_never_reaches_the_network(monkeypatch):
    enable(monkeypatch)
    called = []
    fake_urlopen(monkeypatch, capture=called)

    for bad in ("", None, "919900000001"):
        with pytest.raises(whatsapp.WhatsAppError):
            whatsapp.send_text(bad, "hello")
    assert called == []


def test_12b_no_secret_value_is_hardcoded_in_the_adapter():
    import inspect

    source = inspect.getsource(whatsapp)
    # Names, yes. Values, never.
    assert "WHATSAPP_ACCESS_TOKEN" in source
    assert "EAA" not in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or '"""' in stripped:
            continue
        assert "Bearer EAA" not in stripped


# ===========================================================================
# 13-18  the API route
# ===========================================================================

@pytest.fixture
def wa_env(monkeypatch):
    table, queue = FakeTable(), FakeQueue()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv(
        "ORDERS_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/000000000000/shopflow-orders")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "sqs_client", lambda: queue)
    return table


def seed_quote_job(table, quote, credit=None, customer_id=RAVI):
    job_id = "f" * 32
    result = {"status": "QUOTED", "quote": quote}
    if credit:
        result["credit"] = credit
    table.put_item(Item={
        "PK": f"JOB#{job_id}", "SK": "META", "jobId": job_id,
        "jobType": "ORDER", "status": "DONE", "customerId": customer_id,
        "result": json.dumps(result), "createdAt": 1,
    })
    return job_id


# The shop's workspace is what sends messages, and it asks as the demo owner.
# Every test in this file exercises that path. What an anonymous caller may
# receive from the same route is pinned in tests/test_p1_whatsapp_boundary.py.
OWNER_HEADERS = {"x-shopflow-demo-owner": "demo-workspace"}


def post_send(body, *, owner: bool = True):
    event = {"routeKey": "POST /api/whatsapp/send", "body": json.dumps(body)}
    if owner:
        event["headers"] = dict(OWNER_HEADERS)
    return event


def body_of(response):
    return json.loads(response["body"])


def test_13_with_the_api_disabled_the_route_returns_a_draft(wa_env, quote, credit):
    job_id = seed_quote_job(wa_env, quote, credit)

    body = body_of(api.handler(post_send(
        {"messageType": "QUOTATION", "quoteId": job_id, "customerId": RAVI}), None))

    assert body["status"] == "DRAFT"
    assert body["sent"] is False
    assert body["reason"] == whatsapp.DISABLED
    assert body["draftUrl"].startswith("https://wa.me/")
    assert "22,306.48" in body["text"]
    assert "not configured" in body["notice"]


def test_13b_the_draft_url_carries_the_message_and_the_number(wa_env, quote):
    job_id = seed_quote_job(wa_env, quote)
    body = body_of(api.handler(post_send(
        {"messageType": "QUOTATION", "quoteId": job_id}), None))

    assert body["draftUrl"].startswith("https://wa.me/919900000001?text=")
    # The response itself still shows only a masked number.
    assert body["recipient"] == "+91******0001"


def test_14_with_the_api_enabled_the_message_is_sent(
        wa_env, quote, credit, monkeypatch):
    enable(monkeypatch)
    captured = []
    fake_urlopen(monkeypatch, capture=captured)
    job_id = seed_quote_job(wa_env, quote, credit)

    body = body_of(api.handler(post_send(
        {"messageType": "QUOTATION", "quoteId": job_id}), None))

    assert body["status"] == "SENT"
    assert body["sent"] is True
    assert body["messageId"] == "wamid.TEST"
    assert captured[0]["body"]["text"]["body"] == body["text"]


def test_15_a_send_failure_never_claims_delivery_and_keeps_the_draft(
        wa_env, quote, monkeypatch):
    import io

    enable(monkeypatch)
    fake_urlopen(monkeypatch, error=urllib.error.HTTPError(
        "https://graph.facebook.com/x", 401, "err", {},
        io.BytesIO(json.dumps({"error": {"code": 190}}).encode())))
    job_id = seed_quote_job(wa_env, quote)

    body = body_of(api.handler(post_send(
        {"messageType": "QUOTATION", "quoteId": job_id}), None))

    assert body["status"] == "FAILED"
    assert body["sent"] is False
    assert body["reason"] == whatsapp.AUTH_FAILED
    assert body["draftUrl"].startswith("https://wa.me/")
    assert "draft" in body["notice"].lower()


def test_16_no_token_or_credential_is_ever_returned(
        wa_env, quote, credit, monkeypatch):
    enable(monkeypatch)
    fake_urlopen(monkeypatch)
    job_id = seed_quote_job(wa_env, quote, credit)

    for message_type in ("QUOTATION", "ORDER_CONFIRMATION"):
        raw = api.handler(post_send(
            {"messageType": message_type, "quoteId": job_id}), None)["body"]
        assert TOKEN not in raw
        assert "1234567890" not in raw
        assert "Bearer" not in raw
        assert "authorization" not in raw.lower()


def test_16b_the_response_carries_no_internal_business_data(
        wa_env, quote, credit):
    job_id = seed_quote_job(wa_env, quote, credit)
    raw = api.handler(post_send(
        {"messageType": "QUOTATION", "quoteId": job_id}), None)["body"]

    data = cached_dataset()
    for line in quote["lines"]:
        product = data.product(line["skuId"])
        assert f"{product.costPrice}" not in raw
    for field in ("onHand", "weeklyVelocity", "marginPerRupee",
                  "currentSupplierCost", "supplierId"):
        assert field not in raw


def test_17_sending_mutates_nothing(wa_env, quote, credit, monkeypatch):
    """Not the quotation, not stock, not the balance, not a supplier cost."""
    enable(monkeypatch)
    fake_urlopen(monkeypatch)
    job_id = seed_quote_job(wa_env, quote, credit)

    data = cached_dataset()
    before_table = repr(sorted((k, sorted(v.items())) for k, v in wa_env.items.items()))
    before_stock = {s: data.onHand(s) for s in data.products}
    before_prices = {s: p.sellingPrice for s, p in data.products.items()}
    before_costs = {s: p.costPrice for s, p in data.products.items()}
    before_balances = {c.customerId: c.outstandingAmount
                       for c in data.customers.values()}

    api.handler(post_send({"messageType": "QUOTATION", "quoteId": job_id}), None)
    api.handler(post_send({"messageType": "CREDIT_REMINDER",
                           "customerId": RAVI, "orderTotal": 0}), None)

    after = cached_dataset()
    assert repr(sorted((k, sorted(v.items()))
                       for k, v in wa_env.items.items())) == before_table
    assert {s: after.onHand(s) for s in after.products} == before_stock
    assert {s: p.sellingPrice for s, p in after.products.items()} == before_prices
    assert {s: p.costPrice for s, p in after.products.items()} == before_costs
    assert {c.customerId: c.outstandingAmount
            for c in after.customers.values()} == before_balances


def test_17b_the_quotation_total_is_loaded_not_recalculated(wa_env, quote):
    """A total in the REQUEST is ignored; the stored result is authoritative."""
    job_id = seed_quote_job(wa_env, quote)

    body = body_of(api.handler(post_send({
        "messageType": "QUOTATION", "quoteId": job_id,
        "total": 1.0, "orderTotal": 1.0, "lines": [],
    }), None))

    assert "22,306.48" in body["text"]
    assert "₹1.00" not in body["text"]


def test_18_the_route_validates_its_input(wa_env, quote):
    job_id = seed_quote_job(wa_env, quote)
    for bad in (
        {},
        {"messageType": "MARKETING", "quoteId": job_id},
        {"messageType": "QUOTATION"},
        {"messageType": "QUOTATION", "quoteId": "nope"},
        {"messageType": "QUOTATION", "quoteId": "a" * 32},   # no such job
        {"messageType": "QUOTATION", "quoteId": job_id, "customerId": "../x"},
        {"messageType": "CREDIT_STATUS", "customerId": "CUST-NOT-REAL"},
    ):
        status = api.handler(post_send(bad), None)["statusCode"]
        assert status in (400, 404), bad


def test_18b_an_account_message_needs_no_quotation(wa_env):
    body = body_of(api.handler(post_send(
        {"messageType": "CREDIT_STATUS", "customerId": RAVI,
         "orderTotal": 0}), None))

    assert body["messageType"] == CREDIT_STATUS
    assert body["status"] == "DRAFT"
    assert "Outstanding" in body["text"]


def test_18c_nothing_sends_without_an_explicit_request(wa_env, quote, monkeypatch):
    """There is no trigger, schedule or webhook that can reach this route."""
    enable(monkeypatch)
    captured = []
    fake_urlopen(monkeypatch, capture=captured)

    seed_quote_job(wa_env, quote)
    # Creating the order and polling it must send nothing.
    api.handler({"routeKey": "GET /api/jobs/{jobId}",
                 "pathParameters": {"jobId": "f" * 32}}, None)

    assert captured == []
