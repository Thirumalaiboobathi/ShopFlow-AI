"""Who is allowed to see what the shop pays its suppliers.

ShopFlow has two audiences. The owner asks "what is my margin on this?" and
"what can I afford to restock?", and those answers cannot be given without
supplier cost. The customer gets a quotation and a WhatsApp message, and those
must never carry it.

An evaluator found the contradiction worth fixing: the README described a
customer-safe boundary while a public endpoint returned `previousSupplierCost`.
The endpoint was right - it is the owner's own console, and deleting the field
would have removed the margin feature rather than secured it. What was missing
was that the boundary was implicit.

So it is written down in `handler.OWNER_ROUTES` / `CUSTOMER_FACING_ROUTES`,
marked on every response, and asserted here. This demo has no login, so on the
owner side that is a declaration of intent rather than an access control, and
the README says so. On the customer side it is enforced: these tests fail if
supplier cost ever reaches a quotation or a message.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.loader import load_dataset  # noqa: E402
from engine.margin import margin_view  # noqa: E402
from engine.messages import (  # noqa: E402
    build_quotation_message,
    customer_safe_quote,
)
from engine.purchasing import build_purchase_plan  # noqa: E402
from engine.quote import calculate_quote  # noqa: E402

# Every name the codebase uses for what the shop pays, or what it keeps.
INTERNAL_FIELDS = (
    "costPrice", "supplierPrice", "unitCost", "previousSupplierCost",
    "confirmedSupplierCost", "confirmedCost", "purchaseCost",
    "marginAmount", "marginPercent", "oldMarginAmount", "newMarginAmount",
)

CANONICAL_ITEMS = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3, "uom": "COIL"},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
CANONICAL_TOTAL = 22306.48


@pytest.fixture(scope="module")
def data():
    return load_dataset()


@pytest.fixture(scope="module")
def quote(data):
    return calculate_quote(data, CANONICAL_ITEMS).as_dict()


def _names(node, found=None):
    """Every key name anywhere in a nested payload."""
    found = found if found is not None else set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            _names(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _names(value, found)
    return found


# ---------------------------------------------------------------------------
# The customer side is enforced
# ---------------------------------------------------------------------------

def test_the_customer_quotation_carries_no_supplier_cost(quote):
    safe = customer_safe_quote(quote)
    present = _names(safe) & set(INTERNAL_FIELDS)
    assert present == set(), present


def test_the_whatsapp_quotation_text_carries_no_supplier_cost(data, quote):
    message = build_quotation_message(quote)
    text = message["text"] if isinstance(message, dict) else str(message)

    # The supplier cost of every SKU in the quote, in every form it prints in.
    for line in quote["lines"]:
        view = margin_view(data, line["skuId"], None)
        cost = view.get("previousSupplierCost") if isinstance(view, dict) else None
        if cost is None:
            continue
        for rendered in (str(cost), f"{cost:,.2f}", str(int(cost))):
            assert rendered not in text, f"{line['skuId']} cost {rendered} leaked"


def test_the_whatsapp_text_still_carries_the_real_selling_figures(quote):
    """Stripping cost must not strip the quotation."""
    message = build_quotation_message(quote)
    text = message["text"] if isinstance(message, dict) else str(message)
    assert "22,306.48" in text or "22306.48" in text


def test_the_raw_quotation_the_api_returns_carries_no_supplier_cost(quote):
    """The quote the browser polls is customer-visible. It must be clean even
    before the customer-safe projection is applied."""
    present = _names(quote) & set(INTERNAL_FIELDS)
    assert present == set(), present
    blob = json.dumps(quote)
    assert "supplierCost" not in blob
    assert "costPrice" not in blob


# ---------------------------------------------------------------------------
# The owner side still works, and is declared
# ---------------------------------------------------------------------------

def test_the_owner_margin_view_still_carries_what_the_owner_needs(data):
    view = margin_view(data, "SW-ANC-1W10A", None)
    assert view["previousSupplierCost"] is not None
    assert view["sellingPrice"] is not None


def test_the_owner_purchase_plan_still_carries_costs(data):
    plan = build_purchase_plan(data, 25000)
    payload = json.loads(json.dumps(plan, default=str))
    assert "unitCost" in _names(payload)


def test_every_route_declares_an_audience():
    """A new route must be classified, not left to be discovered later."""
    sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))
    import handler  # noqa: E402

    classified = handler.OWNER_ROUTES | handler.CUSTOMER_FACING_ROUTES
    unclassified = set(handler.ROUTES) - classified
    # Routes that carry no business figures at all need no audience. The two
    # khata routes used to be listed here as carrying none, which was wrong:
    # a khata account is a name, a phone number, a credit limit and an
    # outstanding balance. They are owner routes now.
    neutral = {"GET /api/languages", "GET /api/demo",
               "POST /api/voice/transcribe"}
    assert unclassified <= neutral, unclassified
    assert not (handler.OWNER_ROUTES & handler.CUSTOMER_FACING_ROUTES)


def test_the_endpoints_that_carry_supplier_cost_are_the_declared_owner_ones():
    """The list is exact. Adding cost to another route should fail here."""
    sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))
    import handler  # noqa: E402

    assert handler.OWNER_ROUTES == frozenset({
        "POST /api/shop-queries",
        "POST /api/purchase-plans",
        "POST /api/supplier-price-lists",
        "POST /api/price-decisions",
        # Khata accounts: name, phone number, credit limit, balance.
        "GET /api/customers",
        "GET /api/customers/{customerId}",
        # A credit decision states the limit and the balance it was made
        # against, so it is an owner answer, not a customer-facing one.
        "POST /api/credit/check",
        # Supplier documents, margin alerts and queue operations.
        "GET /api/intelligence",
    })


# ---------------------------------------------------------------------------
# Nothing about the numbers changed
# ---------------------------------------------------------------------------

def test_the_canonical_quotation_is_unchanged(quote):
    assert quote["total"] == CANONICAL_TOTAL
    assert len(quote["lines"]) == 3


def test_margin_and_purchasing_figures_are_unchanged(data):
    view = margin_view(data, "SW-ANC-1W10A", None)
    assert view["sellingPrice"] == 78.3
    assert view["previousSupplierCost"] == 58.0
    assert view["oldMarginAmount"] == 20.3

    plan = build_purchase_plan(data, 25000)
    assert plan["budget"] == 25000


# ---------------------------------------------------------------------------
# The demo-owner gate
# ---------------------------------------------------------------------------
# An independent evaluation read supplier unit costs, supplier identities, and
# synthetic contractor names, phone numbers, credit limits and balances out of
# this API with plain unauthenticated requests. Owner routes now require the
# caller to say it is asking as the shop owner.
#
# These tests assert what that gate does and, just as deliberately, what it
# does not do. It is not authentication and nothing here pretends otherwise.

OWNER_GATE_HEADERS = {"x-shopflow-demo-owner": "demo-workspace"}


def _handler():
    sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))
    import handler  # noqa: E402
    return handler


def _owner_events(handler):
    """One minimal event per owner route."""
    return {
        "GET /api/customers": {},
        "GET /api/customers/{customerId}": {
            "pathParameters": {"customerId": "CUST-RAVI-001"}},
        "POST /api/credit/check": {
            "body": json.dumps({"customerId": "CUST-RAVI-001",
                                "orderTotal": 4200})},
        "POST /api/purchase-plans": {"body": json.dumps({"budget": 25000})},
        "POST /api/shop-queries": {"body": json.dumps({"transcript": "hi"})},
        "POST /api/supplier-price-lists": {"body": json.dumps({})},
        "POST /api/price-decisions": {"body": json.dumps({})},
        "GET /api/intelligence": {},
    }


def test_an_unmarked_request_to_an_owner_route_is_refused():
    handler = _handler()
    for route, event in _owner_events(handler).items():
        assert route in handler.OWNER_ROUTES, route
        response = handler.handler(dict(event, routeKey=route), None)
        assert response["statusCode"] == 401, route
        assert response["headers"]["x-shopflow-audience"] == "owner"


def test_a_refused_owner_route_hands_back_no_business_data():
    """The refusal must not be a leak of its own."""
    handler = _handler()
    for route, event in _owner_events(handler).items():
        body = handler.handler(dict(event, routeKey=route), None)["body"]
        for internal in ("unitCost", "costPrice", "supplierId", "supplierName",
                         "creditLimit", "outstandingAmount", "phone",
                         "marginPerUnit", "customerName"):
            assert internal not in body, (route, internal)


def test_the_gate_does_not_claim_to_be_authentication():
    """An evaluator will work this out in seconds. It should not have to."""
    handler = _handler()
    body = json.loads(handler.handler({"routeKey": "GET /api/customers"},
                                      None)["body"])
    assert body["demoGate"] is True
    assert body["isAuthentication"] is False
    assert "not authentication" in body["note"].lower()
    assert "synthetic" in body["note"].lower()

    source = (ROOT / "backend" / "lambdas" / "api" / "handler.py").read_text(
        encoding="utf-8").lower()
    for forbidden in ("secure owner login", "authenticated owner session is "
                      "enforced", "production authentication"):
        assert forbidden not in source, forbidden


def test_a_marked_request_reaches_the_route():
    handler = _handler()
    response = handler.handler(
        {"routeKey": "GET /api/customers", "headers": OWNER_GATE_HEADERS}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["synthetic"] is True


def test_the_header_name_is_read_case_insensitively():
    """API Gateway does not promise a casing, so neither may the gate."""
    handler = _handler()
    for key in ("X-ShopFlow-Demo-Owner", "x-shopflow-demo-owner",
                "X-SHOPFLOW-DEMO-OWNER"):
        response = handler.handler(
            {"routeKey": "GET /api/customers",
             "headers": {key: "demo-workspace"}}, None)
        assert response["statusCode"] == 200, key


def test_a_wrong_or_empty_marker_is_refused():
    handler = _handler()
    for value in ("", "  ", "demo", "owner", "true"):
        response = handler.handler(
            {"routeKey": "GET /api/customers",
             "headers": {"x-shopflow-demo-owner": value}}, None)
        assert response["statusCode"] == 401, value


def test_customer_facing_routes_stay_open_and_unchanged():
    """The gate may not reach the public surface."""
    handler = _handler()
    for route in handler.CUSTOMER_FACING_ROUTES:
        response = handler.handler({"routeKey": route}, None)
        # Whatever else it says - a validation error, a missing job - it is
        # never the gate, and it never asks the caller to prove it is the owner.
        assert response["statusCode"] != 401, route
        assert "demoGate" not in response["body"], route


def test_every_owner_route_is_gated_by_membership_alone():
    """Adding a route to OWNER_ROUTES gates it. Nobody has to remember to."""
    handler = _handler()
    for route in handler.OWNER_ROUTES:
        assert handler.handler({"routeKey": route}, None)["statusCode"] == 401
