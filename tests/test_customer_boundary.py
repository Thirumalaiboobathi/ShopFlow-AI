"""What a customer may read back from a job, and what they may never read.

A live evaluator found this, and it was real. The owner confirmed a supplier
price - Finolex 1.5 sqmm red 90m at Rs 6,300 - and an anonymous request for a
fresh customer order,

    GET /api/jobs/{jobId}

returned the shop's previous supplier cost, its confirmed supplier cost, the
old and new margin, a suggested internal selling price and the whole
`marginProtection` panel. The worker attaches that panel to the stored result
for the owner, and the customer-facing route returned the stored result whole.
The same route was also handing out a khata customer's credit limit and
outstanding balance.

The fix is at the response boundary: an allow-list of what a customer's order
result may carry, then a recursive scrub of owner-only keys at every depth.

These tests do not check a list of known field names and stop. They walk the
entire customer response, at every depth, against three independent tests:

  * an explicit denylist of owner field NAMES
  * a check that no owner-only OBJECT survives anywhere
  * a check that no owner FIGURE survives as a value (5,900, 6,300, 708 ...)

and they run the evaluator's exact scenario through the real worker and the
real API route, so a leak introduced anywhere along that path fails here.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))

import handler as api  # noqa: E402

OWNER_HEADERS = {"x-shopflow-demo-owner": "demo-workspace"}

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"
JOB = "c" * 32
KHATA = "CUST-RAVI-001"

CANONICAL = ("Anna, 20 Anchor modular switches 1-Way 10A, "
             "3 coils Finolex 1.5 sq mm red wire 90m, "
             "2 Havells MCB SP 32A.")
CANONICAL_TOTAL = 22306.48

PREVIOUS_SUPPLIER_COST = 5900.0
CONFIRMED_SUPPLIER_COST = 6300.0

# Owner field names that must never appear in a customer response, at any
# depth. The brief's own list, plus the credit and PII fields the same route
# was found to carry.
DENYLIST = frozenset({
    "previousSupplierCost", "confirmedSupplierCost", "unitCost", "margin",
    "marginPct", "marginProtection", "suggestedSellingPrice", "supplierPrice",
    "supplierCost", "costPrice", "oldMarginAmount", "newMarginAmount",
    "oldMarginPercent", "newMarginPercent", "marginReductionAmount",
    "marginReductionPercent", "marginWarningPercent", "suggestionNote",
    "creditLimit", "currentOutstanding", "projectedOutstanding",
    "outstandingAmount", "remainingCredit", "phone", "customerName",
    "customerId", "budget", "plannedSpend", "restockCost", "credit",
})

# Whole objects that belong to the owner. None may survive, at any depth.
OWNER_OBJECTS = frozenset({"marginProtection", "credit", "plan", "review",
                           "decisions", "confirmedCosts", "whatIf"})

# Fragments of a key that mark owner economics, whatever the exact spelling.
OWNER_FRAGMENTS = ("supplier", "cost", "margin", "suggestedselling",
                   "creditlimit", "outstanding", "phone", "budget")

# Owner figures from this scenario. None may survive as a value either - a
# renamed field carrying 6300 is the same leak with a different label.
OWNER_FIGURES = frozenset({
    PREVIOUS_SUPPLIER_COST, CONFIRMED_SUPPLIER_COST,
    708.0, 308.0,            # Finolex margin before and after
    7055.66,                 # suggested selling price
    15000.0, 8500.0,         # CUST-RAVI-001 credit limit and outstanding
})


# ---------------------------------------------------------------------------
# Walking a response
# ---------------------------------------------------------------------------

def walk(value, path=""):
    """Every (path, key, value) in a JSON value, at every depth."""
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key, child
            yield from walk(child, here)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, f"{path}[]")


def owner_leaks(response: dict) -> list:
    """Every owner field, object or figure anywhere in `response`."""
    leaks = []
    for path, key, value in walk(response):
        flat = re.sub(r"[^a-z]", "", str(key).lower())
        if key in DENYLIST:
            leaks.append(f"denylisted field {path}")
        if key in OWNER_OBJECTS:
            leaks.append(f"owner object {path}")
        if any(fragment in flat for fragment in OWNER_FRAGMENTS):
            leaks.append(f"owner key {path}")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and float(value) in OWNER_FIGURES
                and key not in ("elapsedMs",)):
            leaks.append(f"owner figure {path}={value}")
    return leaks


# ---------------------------------------------------------------------------
# The real worker and the real API route, sharing one store
# ---------------------------------------------------------------------------

class Store:
    """One in-memory table both Lambdas read and write, as DynamoDB would."""

    def __init__(self, confirmed_cost=None):
        self.items = {}
        if confirmed_cost is not None:
            self.items[("SHOP#demo", f"COST#{WIRE}")] = {
                "PK": "SHOP#demo", "SK": f"COST#{WIRE}", "skuId": WIRE,
                "confirmedCost": confirmed_cost, "confirmedAt": 1_758_000_000,
                "sourceJobId": "d" * 32}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, **kw):
        item = self.items.setdefault((Key["PK"], Key["SK"]), dict(Key))
        values = kw.get("ExpressionAttributeValues") or {}
        if "#status IN" in (kw.get("ConditionExpression") or ""):
            item["status"] = values[":processing"]
            return {}
        for placeholder, name in (kw.get("ExpressionAttributeNames") or {}).items():
            item[name] = values[f":{placeholder[1:]}"]
        return {}

    def query(self, KeyConditionExpression, **_kw):
        from boto3.dynamodb.conditions import And, BeginsWith, Equals
        pk = prefix = None
        stack = [KeyConditionExpression]
        while stack:
            node = stack.pop()
            if isinstance(node, And):
                stack.extend(node._values)
            elif isinstance(node, Equals):
                pk = node._values[1]
            elif isinstance(node, BeginsWith):
                prefix = node._values[1]
        return {"Items": [dict(v) for (p, s), v in self.items.items()
                          if p == pk and (prefix is None or s.startswith(prefix))]}


class FakeBedrock:
    def __init__(self, turns):
        self.turns = list(turns)

    def converse(self, **_kw):
        return {"output": {"message": self.turns.pop(0)}}


def tool_uses(*calls):
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": f"t{i}", "name": name, "input": args}}
        for i, (name, args) in enumerate(calls, 1)]}


CANONICAL_SCRIPT = [
    tool_uses(
        ("search_catalog", {"requestedText": "20 Anchor modular switches 1-Way 10A",
                            "brand": "Anchor", "category": "Switch",
                            "specification": "1-Way 10A"}),
        ("search_catalog", {"requestedText": "3 coils Finolex 1.5 sq mm red wire 90m",
                            "brand": "Finolex", "category": "Wire",
                            "colour": "Red", "length": "90m", "uom": "COIL"}),
        ("search_catalog", {"requestedText": "2 Havells MCB SP 32A",
                            "brand": "Havells", "category": "MCB",
                            "specification": "SP 32A"})),
    tool_uses(("calculate_quote", {"items": [
        {"skuId": SWITCH, "quantity": 20},
        {"skuId": WIRE, "quantity": 3, "uom": "COIL"},
        {"skuId": MCB, "quantity": 2}]})),
]


@pytest.fixture
def run_order(monkeypatch):
    """Place one order through the REAL worker; return the shared store.

    The agent is the real `run_order_agent` driven by a scripted model, so
    the stored result has exactly the shape production writes - including
    the owner's margin panel and khata credit, when they apply.
    """
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setenv("ORDERS_QUEUE_URL", "q")
    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)

    def _run(*, confirmed_cost=None, customer_id=None, script=CANONICAL_SCRIPT,
             order_text=CANONICAL):
        store = Store(confirmed_cost)
        store.items[(f"JOB#{JOB}", "META")] = {
            "PK": f"JOB#{JOB}", "SK": "META", "jobId": JOB, "jobType": "ORDER",
            "status": "QUEUED", "orderText": order_text, "language": "en",
            "createdAt": 1_758_000_000,
            **({"customerId": customer_id} if customer_id else {})}

        import boto3

        class _Resource:
            def Table(self, _name):
                return store

        monkeypatch.setattr(boto3, "resource", lambda *a, **k: _Resource())
        sys.modules.pop("lambdas.worker.handler", None)
        worker = importlib.import_module("lambdas.worker.handler")
        monkeypatch.setattr(worker, "_table", store)

        real_agent = worker.run_order_agent
        monkeypatch.setattr(worker, "run_order_agent", lambda data, text, **kw: real_agent(
            data, text, client=FakeBedrock(list(script)),
            **{k: v for k, v in kw.items() if k != "client"}))

        worker.handler({"Records": [{"messageId": "m", "body": json.dumps(
            {"jobId": JOB, "jobType": "ORDER", "version": 1})}]}, None)
        monkeypatch.setattr(api, "table", lambda: store)
        return store

    return _run


def poll(headers=None, job_id=JOB):
    event = {"routeKey": "GET /api/jobs/{jobId}",
             "pathParameters": {"jobId": job_id}}
    if headers is not None:
        event["headers"] = headers
    response = api.handler(event, None)
    return response, json.loads(response["body"])


def stored_result(store) -> dict:
    return json.loads(store.items[(f"JOB#{JOB}", "META")]["result"])


# ---------------------------------------------------------------------------
# 1-3. before and after confirmation, with the canonical supplier story
# ---------------------------------------------------------------------------

def test_1_a_customer_poll_before_any_supplier_confirmation_is_clean(run_order):
    store = run_order(confirmed_cost=None)

    response, body = poll()

    assert response["statusCode"] == 200
    assert body["result"]["status"] == "QUOTED"
    assert body["result"]["quote"]["total"] == CANONICAL_TOTAL
    assert owner_leaks(body) == []
    # Nothing was confirmed, so there was no panel to leak in the first place.
    assert "marginProtection" not in stored_result(store)


def test_2_a_customer_poll_after_supplier_confirmation_is_clean(run_order):
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)

    # The scenario is real: the stored result DOES carry the owner's panel.
    assert "marginProtection" in stored_result(store)

    _response, body = poll()
    assert owner_leaks(body) == []
    assert body["result"]["quote"]["total"] == CANONICAL_TOTAL


def test_3_the_5900_to_6300_story_is_in_the_store_and_not_in_the_response(run_order):
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)

    affected = stored_result(store)["marginProtection"]["affected"]
    finolex = next(a for a in affected if a["skuId"] == WIRE)
    assert finolex["previousSupplierCost"] == PREVIOUS_SUPPLIER_COST
    assert finolex["confirmedSupplierCost"] == CONFIRMED_SUPPLIER_COST

    _response, body = poll()
    served = json.dumps(body)
    for field in ("previousSupplierCost", "confirmedSupplierCost"):
        assert field not in served
    figures = [v for _p, _k, v in walk(body)
               if isinstance(v, float) and v in (PREVIOUS_SUPPLIER_COST,
                                                 CONFIRMED_SUPPLIER_COST)]
    assert figures == []


# ---------------------------------------------------------------------------
# 4 and 7. the whole response, recursively, against the denylist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", sorted(DENYLIST))
def test_4_no_denylisted_field_appears_anywhere_in_the_customer_response(
        run_order, field):
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)
    _response, body = poll()
    assert [p for p, k, _v in walk(body) if k == field] == []


def test_7_no_owner_object_survives_at_any_depth(run_order):
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)
    _response, body = poll()
    assert [p for p, k, _v in walk(body) if k in OWNER_OBJECTS] == []


def test_7b_the_walker_would_see_a_leak_if_there_were_one(run_order):
    """A recursive test that cannot find anything proves nothing on its own."""
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)
    leaks = owner_leaks(stored_result(store))
    assert any("marginProtection" in leak for leak in leaks)
    assert any("creditLimit" in leak for leak in leaks)
    assert any("previousSupplierCost" in leak for leak in leaks)


# ---------------------------------------------------------------------------
# 5-6. owner and customer, independently
# ---------------------------------------------------------------------------

def test_5_the_owner_still_receives_the_margin_panel_and_the_khata(run_order):
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)

    response, body = poll(OWNER_HEADERS)

    assert response["statusCode"] == 200
    assert response["headers"]["x-shopflow-audience"] == "owner"
    finolex = next(a for a in body["result"]["marginProtection"]["affected"]
                   if a["skuId"] == WIRE)
    assert finolex["previousSupplierCost"] == PREVIOUS_SUPPLIER_COST
    assert finolex["confirmedSupplierCost"] == CONFIRMED_SUPPLIER_COST
    assert finolex["oldMarginAmount"] == 708.0
    assert finolex["newMarginAmount"] == 308.0
    assert body["result"]["credit"]["creditLimit"] == 15000.0


def test_6_customer_and_owner_views_of_one_job_differ_only_by_owner_data(run_order):
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)

    customer_response, customer = poll()
    owner_response, owner = poll(OWNER_HEADERS)

    assert customer_response["headers"]["x-shopflow-audience"] == "customer"
    assert owner_response["headers"]["x-shopflow-audience"] == "owner"
    # The commercial quotation is the same quotation for both: same lines,
    # same quantities, same prices, same totals, same GST. The customer's
    # lines simply carry fewer fields - no stock count, no sales velocity, no
    # cover, no evidence string that spells out the stock.
    customer_quote, owner_quote = (customer["result"]["quote"],
                                   owner["result"]["quote"])
    assert customer_quote["total"] == owner_quote["total"] == CANONICAL_TOTAL
    # The same GST, figure for figure. Only the internal module name in the
    # evidence is dropped from the customer's copy.
    owner_gst = dict(owner_quote["gst"],
                     evidence={k: v for k, v in owner_quote["gst"]["evidence"].items()
                               if k != "source"})
    assert customer_quote["gst"] == owner_gst
    for c_line, o_line in zip(customer_quote["lines"], owner_quote["lines"]):
        assert set(c_line) < set(o_line)
        assert {k: o_line[k] for k in c_line if k != "evidence"} == \
            {k: v for k, v in c_line.items() if k != "evidence"}
        for internal in ("onHand", "shortageQty", "weeklyVelocity",
                         "coverageWeeks"):
            assert internal not in c_line and internal in o_line
    # The customer's trace is the public reading of the owner's.
    from agent import decision_trace
    assert customer["result"]["decisionTrace"] == \
        decision_trace.public_view(owner["result"]["decisionTrace"])
    # And everything the owner has that the customer lacks is owner or
    # internal data: the margin panel, the khata position, the raw tool-call
    # audit record, the model's own search arguments, and the model run.
    missing = set(owner["result"]) - set(customer["result"])
    assert missing == {"marginProtection", "credit", "trace", "matches",
                       "modelId", "turns", "elapsedMs"}


# ---------------------------------------------------------------------------
# 8. the evaluator's exact scenario
# ---------------------------------------------------------------------------

def test_8_evaluator_scenario_confirmed_finolex_then_fresh_customer_order(run_order):
    """Confirm Finolex 1.5 sqmm red 90m at Rs 6,300; order; poll anonymously.

    The evaluator also sent `X-Shopflow-Audience: customer` on the request,
    which is sent here too. It is a response header ShopFlow sets, not one a
    caller chooses, and it must neither unlock nor break anything.
    """
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)

    response, body = poll({"X-Shopflow-Audience": "customer"})

    assert response["statusCode"] == 200
    assert body["result"]["status"] == "QUOTED"
    assert body["result"]["quote"]["total"] == CANONICAL_TOTAL
    assert len(body["result"]["quote"]["lines"]) == 3
    assert owner_leaks(body) == []
    # The owner's data is still there for the owner: nothing was deleted.
    assert "marginProtection" in stored_result(store)


def test_8b_a_caller_cannot_promote_itself_with_the_audience_header(run_order):
    """Asking to be the owner in the audience header grants nothing."""
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)

    for spoofed in ({"X-Shopflow-Audience": "owner"},
                    {"x-shopflow-audience": "owner"},
                    {"x-shopflow-demo-owner": "owner"},
                    {"x-shopflow-demo-owner": ""}):
        response, body = poll(spoofed)
        assert response["headers"]["x-shopflow-audience"] == "customer", spoofed
        assert owner_leaks(body) == [], spoofed


# ---------------------------------------------------------------------------
# The khata leak on the same route
# ---------------------------------------------------------------------------

def test_a_khata_orders_credit_position_is_not_customer_facing(run_order):
    store = run_order(customer_id=KHATA)
    assert stored_result(store)["credit"]["creditLimit"] == 15000.0

    _response, body = poll()
    served = json.dumps(body)
    for field in ("creditLimit", "currentOutstanding", "projectedOutstanding",
                  "customerId", KHATA):
        assert field not in served, field
    assert "credit" not in body["result"]
    assert "credit" not in (body["result"].get("localized") or {})


# ---------------------------------------------------------------------------
# Default-deny: what the allow-list is for
# ---------------------------------------------------------------------------

def test_an_owner_field_added_tomorrow_is_absent_by_default(run_order):
    """The worker grows a field; the customer does not see it until decided."""
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)
    row = store.items[(f"JOB#{JOB}", "META")]
    result = json.loads(row["result"])
    result["supplierLedger"] = {"lastPaid": 6300.0}
    result["purchasingNotes"] = "reorder next week"
    result["anythingNew"] = {"value": 1}
    row["result"] = json.dumps(result)

    _response, body = poll()
    for key in ("supplierLedger", "purchasingNotes", "anythingNew"):
        assert key not in body["result"], key


def test_an_owner_figure_nested_inside_an_allowed_object_is_scrubbed(run_order):
    """A cost on a quote line would ride out inside the quote. It may not."""
    store = run_order()
    row = store.items[(f"JOB#{JOB}", "META")]
    result = json.loads(row["result"])
    result["quote"]["lines"][0]["costPrice"] = 5900.0
    result["quote"]["lines"][0]["unitCost"] = 5900.0
    result["matches"][0]["supplierPrice"] = 6300.0
    row["result"] = json.dumps(result)

    _response, body = poll()
    assert owner_leaks(body) == []
    # And the commercial figures beside them are untouched.
    assert body["result"]["quote"]["total"] == CANONICAL_TOTAL


def test_viewing_a_job_changes_nothing_that_is_stored(run_order):
    store = run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST, customer_id=KHATA)
    before = store.items[(f"JOB#{JOB}", "META")]["result"]
    poll()
    poll(OWNER_HEADERS)
    assert store.items[(f"JOB#{JOB}", "META")]["result"] == before


def test_the_customer_view_keeps_everything_a_customer_needs(run_order):
    """Removing the owner's data must not remove the quotation with it."""
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)
    _response, body = poll()

    result = body["result"]
    assert result["status"] == "QUOTED"
    assert result["quote"]["total"] == CANONICAL_TOTAL
    lines = {l["skuId"]: l for l in result["quote"]["lines"]}
    # Whether each line is in stock - yes; how many units the shop holds - no.
    assert (lines[SWITCH]["quantity"], lines[SWITCH]["inStock"]) == (20, False)
    assert (lines[WIRE]["quantity"], lines[WIRE]["inStock"]) == (3, False)
    assert (lines[MCB]["quantity"], lines[MCB]["inStock"]) == (2, True)
    assert all("sellingPrice" in l and "lineTotal" in l for l in lines.values())
    assert result["quote"]["gst"]["grandTotal"] == 26321.64
    assert result["decisionTrace"]["available"] is True
    assert result["summary"]


def test_the_customer_view_carries_no_reasoning_and_no_prompt(run_order):
    run_order(confirmed_cost=CONFIRMED_SUPPLIER_COST)
    _response, body = poll()
    served = json.dumps(body).lower()
    for forbidden in ("<thinking", "system prompt", "you are the order desk",
                      "apikey", "api_key", "secret"):
        assert forbidden not in served, forbidden


# ---------------------------------------------------------------------------
# The rest of the job route
# ---------------------------------------------------------------------------

def test_a_price_list_job_is_still_refused_to_a_customer(monkeypatch):
    store = Store()
    job = "e" * 32
    store.items[(f"JOB#{job}", "META")] = {
        "PK": f"JOB#{job}", "SK": "META", "jobId": job, "jobType": "PRICE_LIST",
        "status": "DONE", "createdAt": 1_758_000_000,
        "result": json.dumps({"review": {"lines": [{"comparison": {
            "previousPrice": 5900.0, "currentPrice": 6300.0}}]}})}
    monkeypatch.setattr(api, "table", lambda: store)

    response, body = poll(job_id=job)
    assert response["statusCode"] == 401
    assert "6300" not in json.dumps(body)


def test_a_price_list_poll_by_the_owner_is_labelled_owner(monkeypatch):
    """It used to come back labelled `customer` - correct data, wrong label."""
    store = Store()
    job = "e" * 32
    store.items[(f"JOB#{job}", "META")] = {
        "PK": f"JOB#{job}", "SK": "META", "jobId": job, "jobType": "PRICE_LIST",
        "status": "DONE", "createdAt": 1_758_000_000,
        "result": json.dumps({"review": {"lines": []}})}
    monkeypatch.setattr(api, "table", lambda: store)

    response, _body = poll(OWNER_HEADERS, job_id=job)
    assert response["statusCode"] == 200
    assert response["headers"]["x-shopflow-audience"] == "owner"


def test_every_customer_facing_route_is_still_open():
    """The boundary narrows what is returned. It gates nothing new."""
    assert "GET /api/jobs/{jobId}" in api.CUSTOMER_FACING_ROUTES
    assert "GET /api/jobs/{jobId}" not in api.OWNER_ROUTES


def test_the_allow_list_carries_no_owner_field():
    """The allow-list itself may not be the leak."""
    for field in api.CUSTOMER_RESULT_FIELDS:
        assert field not in DENYLIST, field
        assert field not in OWNER_OBJECTS, field


# ---------------------------------------------------------------------------
# P1-D: the customer's prose - no tool errors, no instructions to the model
# ---------------------------------------------------------------------------
# Probed before fixing: in each of these four scenarios the customer summary
# was already good, deterministic text - but the same response carried the raw
# tool trace ("These SKU ids do not exist ... Use only ids returned by
# search_catalog"), the instructions written to the model inside each match
# ("Do not convert the quantity ... call request_clarification"), and on a
# failure, "The agent repeatedly produced invalid tool arguments".

def _prose(text):
    return {"role": "assistant", "content": [{"text": text}]}


P1D_SCENARIOS = {
    "unknown brand": ("5 Siemens contactor 40A", [
        tool_uses(("search_catalog", {"requestedText": "5 Siemens contactor 40A",
                                      "brand": "Siemens"})),
        _prose("We do not stock Siemens.")]),
    "wrong unit": ("2 boxes of Finolex 1.5 sqmm red wire 90m", [
        tool_uses(("search_catalog", {
            "requestedText": "2 boxes of Finolex 1.5 sqmm red wire 90m",
            "brand": "Finolex", "category": "Wire", "colour": "Red",
            "length": "90m", "uom": "BOX"})),
        _prose("Boxes are not available.")]),
    "invalid product": ("20 Anchor modular switches 1-Way 10A", [
        tool_uses(("search_catalog", {
            "requestedText": "20 Anchor modular switches 1-Way 10A",
            "brand": "Anchor", "category": "Switch",
            "specification": "1-Way 10A"}))] + [
        tool_uses(("calculate_quote", {"items": [
            {"skuId": "SW-FAKE-999", "quantity": 20}]}))] * 3),
    "quantity mismatch": ("2 Havells MCB SP 32A", [
        tool_uses(("search_catalog", {"requestedText": "2 Havells MCB SP 32A",
                                      "brand": "Havells", "category": "MCB",
                                      "specification": "SP 32A"})),
        tool_uses(("calculate_quote", {"items": [{"skuId": MCB,
                                                  "quantity": 4}]}))]),
}

# Text that is about the machinery, not about the customer's order.
INTERNAL_TEXT = ("must be a positive integer", "do not exist", "Use only ids",
                 "search_catalog", "calculate_quote", "request_clarification",
                 "Do not invent", "Do not convert", "ToolError", "Traceback",
                 "invalid tool arguments", "skuId yourself",
                 # tool-call identifiers and raw tool blocks
                 "toolUseId", "toolUse", "toolResult", "errorKind",
                 # model reasoning
                 "<thinking", "</thinking")


@pytest.mark.parametrize("label", sorted(P1D_SCENARIOS))
def test_p1d_no_internal_tool_text_reaches_a_customer(run_order, label):
    text, script = P1D_SCENARIOS[label]
    run_order(script=script, order_text=text)

    _response, body = poll()
    served = json.dumps(body)
    assert [s for s in INTERNAL_TEXT if s in served] == [], label
    assert "trace" not in body["result"]
    assert owner_leaks(body) == []


@pytest.mark.parametrize("label", sorted(P1D_SCENARIOS))
def test_p1d_the_owner_keeps_the_full_audit_record(run_order, label):
    """Nothing was deleted - the owner still sees every tool call and error."""
    text, script = P1D_SCENARIOS[label]
    run_order(script=script, order_text=text)

    _response, body = poll(OWNER_HEADERS)
    assert body["result"]["trace"], label
    assert any(m.get("instruction") for m in body["result"]["matches"]), label


@pytest.mark.parametrize("label", ["unknown brand", "wrong unit",
                                   "quantity mismatch"])
def test_p1d_a_normal_clarification_is_never_described_as_a_failure(run_order,
                                                                    label):
    text, script = P1D_SCENARIOS[label]
    run_order(script=script, order_text=text)

    _response, body = poll()
    result = body["result"]
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["quote"] is None
    for field in (result["summary"], result.get("message") or ""):
        lowered = field.lower()
        for word in ("failed", "error", "could not complete", "invalid"):
            assert word not in lowered, (label, field)


def test_p1d_the_wrong_unit_question_names_the_real_unit(run_order):
    """It may not claim an incorrect unit. The catalogue says coils."""
    text, script = P1D_SCENARIOS["wrong unit"]
    run_order(script=script, order_text=text)

    _response, body = poll()
    summary = body["result"]["summary"]
    assert "coil" in summary
    assert "box" in summary


def test_p1d_an_unknown_brand_is_not_given_invented_product_facts(run_order):
    text, script = P1D_SCENARIOS["unknown brand"]
    run_order(script=script, order_text=text)

    _response, body = poll()
    result = body["result"]
    assert result["quote"] is None
    assert result["clarification"]["options"] == []
    assert "Siemens" in result["summary"]
    assert "not in the catalogue" in result["summary"]


def test_p1d_a_failed_order_tells_the_customer_plainly(run_order):
    text, script = P1D_SCENARIOS["invalid product"]
    run_order(script=script, order_text=text)

    _response, customer = poll()
    _response, owner = poll(OWNER_HEADERS)

    assert customer["result"]["status"] == "FAILED"
    assert customer["result"]["message"] == api.CUSTOMER_FAILURE_MESSAGE
    # The owner still gets the real reason.
    assert "invalid tool arguments" in owner["result"]["message"]


def test_p1d_model_facing_keys_are_removed_at_any_depth():
    body = {"jobType": "ORDER", "result": {
        "status": "QUOTED", "matches": [{
            "status": "RESOLVED", "instruction": "Use this skuId.",
            "diagnostic": {"notes": ["call request_clarification"]},
            "lineIsolation": {"blocking": ["search_catalog ..."],
                              "changes": [{"reason": "the line names Anchor"}]}}]}}
    view = api.customer_job_view(body)
    # Not scrubbed field by field any more: the whole search record is the
    # model's arguments and the guards' corrections of them, and none of it
    # is the customer's. It is absent from the customer view entirely.
    assert "matches" not in view["result"]
    served = json.dumps(view)
    for internal in ("instruction", "diagnostic", "blocking", "lineIsolation",
                     "the line names Anchor"):
        assert internal not in served
    # And the input is untouched.
    assert body["result"]["matches"][0]["instruction"] == "Use this skuId."


# ---------------------------------------------------------------------------
# Voice-transcript jobs get their own allow-list too
# ---------------------------------------------------------------------------
# Before this, a transcript result went through the recursive scrub only. It
# held no owner data - probed first, and the customer and owner views were
# identical - but "holds none today" is not default-deny. A transcript result
# is produced in one place and has four fields; those four, plus the language
# block, are all a customer receives. The voice interface reads `transcript`.

from test_transcribe import (post_audio, set_transcript,  # noqa: E402
                             voice_env)  # noqa: F401
# The voice fixture patches the handler as `lambdas.api.handler`, which is a
# different module object from the `handler` imported at the top of this file.
# These tests must drive the one the fixture patched, or the upload goes to
# real S3 instead of the fake - which is exactly what a first draft did.
from test_transcribe import api as voice_api  # noqa: E402

SPOKEN = "Anna 20 Anchor modular switches 1-Way 10A"


def _transcript_job(voice_env, monkeypatch, text=SPOKEN):
    _table, _s3, transcribe, _queue = voice_env
    set_transcript(monkeypatch, text)
    job = json.loads(voice_api.handler(post_audio(), None)["body"])["jobId"]
    transcribe.state = "COMPLETED"
    return job


def voice_poll(job_id, headers=None):
    event = {"routeKey": "GET /api/jobs/{jobId}",
             "pathParameters": {"jobId": job_id}}
    if headers is not None:
        event["headers"] = headers
    response = voice_api.handler(event, None)
    return response, json.loads(response["body"])


@pytest.mark.parametrize("which", ["first poll", "repeat poll"])
def test_a_transcript_poll_carries_only_allow_listed_fields(voice_env,
                                                           monkeypatch, which):
    job = _transcript_job(voice_env, monkeypatch)
    if which == "repeat poll":
        voice_poll(job)             # the first poll stores the result

    response, body = voice_poll(job)

    assert response["statusCode"] == 200
    assert response["headers"]["x-shopflow-audience"] == "customer"
    assert set(body["result"]) <= api.CUSTOMER_TRANSCRIPT_FIELDS
    # The one field the voice interface reads, intact.
    assert body["result"]["transcript"] == SPOKEN
    assert owner_leaks(body) == []


def test_a_transcript_is_not_given_an_order_trace(voice_env, monkeypatch):
    """A transcript is not an order; "0 product lines detected" misdescribes it."""
    job = _transcript_job(voice_env, monkeypatch)
    voice_poll(job)
    _response, body = voice_poll(job)
    assert "decisionTrace" not in body["result"]


def test_the_owner_transcript_view_is_unchanged(voice_env, monkeypatch):
    job = _transcript_job(voice_env, monkeypatch)
    voice_poll(job)
    response, body = voice_poll(job, OWNER_HEADERS)
    assert response["headers"]["x-shopflow-audience"] == "owner"
    assert body["result"]["transcript"] == SPOKEN
    assert "decisionTrace" in body["result"]


def test_an_owner_field_placed_on_a_transcript_never_reaches_a_customer(
        voice_env, monkeypatch):
    """Default-deny, proved: whatever lands on the stored result stays there."""
    table, _s3, _transcribe, _queue = voice_env
    job = _transcript_job(voice_env, monkeypatch)
    voice_poll(job)
    row = table.items[(f"JOB#{job}", "META")]
    stored = json.loads(row["result"])
    stored.update({
        "marginProtection": {"affected": [{"previousSupplierCost": 5900.0}]},
        "credit": {"creditLimit": 15000.0, "currentOutstanding": 8500.0},
        "supplierCost": 6300.0,
        "anythingNew": {"value": 1},
    })
    row["result"] = json.dumps(stored)

    _response, body = voice_poll(job)
    assert set(body["result"]) <= api.CUSTOMER_TRANSCRIPT_FIELDS
    assert owner_leaks(body) == []
    assert body["result"]["transcript"] == SPOKEN


def test_a_failed_transcript_tells_the_customer_nothing_internal(voice_env,
                                                                 monkeypatch):
    def explode(_uri):
        raise RuntimeError("s3 GetObject denied: arn:aws:s3:::internal-bucket")

    _table, _s3, transcribe, _queue = voice_env
    monkeypatch.setattr(voice_api, "_fetch_transcript", explode)
    job = json.loads(voice_api.handler(post_audio(), None)["body"])["jobId"]
    transcribe.state = "COMPLETED"

    _response, body = voice_poll(job)
    assert body["status"] == "FAILED"
    assert body["error"] == "The transcript could not be read. Please try again."
    served = json.dumps(body)
    for internal in ("RuntimeError", "arn:aws", "internal-bucket", "Traceback"):
        assert internal not in served, internal


def test_a_job_type_with_no_customer_allow_list_returns_no_result():
    """Default-deny applies to job types, not only to fields."""
    view = api.customer_job_view({
        "jobId": "f" * 32, "jobType": "SOMETHING_NEW", "status": "DONE",
        "result": {"supplierCost": 6300.0, "harmless": "x"}})
    assert "result" not in view
    assert view["status"] == "DONE"


def test_every_customer_allow_list_is_free_of_owner_fields():
    for job_type, fields in api.CUSTOMER_FIELDS_BY_JOB.items():
        for field in fields:
            assert field not in DENYLIST, (job_type, field)
            assert field not in OWNER_OBJECTS, (job_type, field)
    # A price list has no customer view at all; it is refused before one exists.
    assert "PRICE_LIST" not in api.CUSTOMER_FIELDS_BY_JOB
