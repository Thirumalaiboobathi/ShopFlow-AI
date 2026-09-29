"""Remediation of a strict independent evaluation.

Four things are pinned here:

  1. Supplier price-list plausibility. A price that is not a finite positive
     number, or is above a hard ceiling, is not read as a price. One outside
     0.5x-3x of what the shop last paid is EXTREME_CHANGE: it waits for the
     owner and raises no alert at read time. ₹5,900 -> ₹6,300 is untouched.
  2. Spoken ratings in words ("thirty two amp") - read only directly before
     amp, only 1-99; everything else is left for the matcher, which asks.
  3. The API boundary: owner routes, customer views, job ids, and request
     fields a caller may not supply.
  4. The business figures every demo depends on, unchanged.

(The client-supplied SKU on /api/orders is in tests/test_confirmed_choice.py.)
"""

from __future__ import annotations

import json
import math

import pytest

import lambdas.api.handler as api
from engine import supplier_prices as spx
from engine import supplier_reply as sr
from engine.loader import cached_dataset
from engine.margin import margin_view
from engine.matching import AMBIGUOUS, RESOLVED, normalize_ratings, resolve_product
from engine.purchasing import InvalidBudgetError, apply_confirmed_costs, build_purchase_plan
from engine.quote import calculate_quote
from engine import gst
from engine.voice import normalize_transcript
from test_api import FakeQueue, FakeTable
from test_queue import worker  # noqa: F401

WIRE, SWITCH, MCB = "W-FIN-1.5-RED-90M", "SW-ANC-1W10A", "MCB-HAV-SP-32A-C"
WIRE_ROW = "Finolex 1.5 sqmm FR Wire Red 90m coil"
OWNER = {api.DEMO_OWNER_HEADER: api.DEMO_OWNER_VALUE}


@pytest.fixture(scope="module")
def shop():
    return cached_dataset()


def review(shop, *rows):
    return spx.review_price_list(shop, "Sri Balaji Electricals", "2026-09-29",
                                 [{"description": d, "price": p} for d, p in rows])


def wire_row(result):
    return next(r for r in result.results if r.skuId == WIRE)


# ---------------------------------------------------------------------------
# 1. supplier price-list plausibility
# ---------------------------------------------------------------------------

def test_the_live_rise_is_exactly_as_before(shop):
    row = wire_row(review(shop, (WIRE_ROW, 6300)))
    c = row.comparison.as_dict()
    assert (c["previousPrice"], c["currentPrice"], c["percentageDelta"]) == (
        5900.0, 6300.0, 6.78)
    assert c["materialChange"] is True and c["plausibility"] == spx.WITHIN_RANGE
    assert row.reviewState == spx.STATE_REVIEW_REQUIRED


@pytest.mark.parametrize("price,plausibility,material", [
    (5605.0, spx.WITHIN_RANGE, True),         # -5% decrease
    (5900.0, spx.WITHIN_RANGE, False),        # unchanged
    (2950.0, spx.WITHIN_RANGE, True),         # 0.5x - the lower bound, inclusive
    (11800.0, spx.WITHIN_RANGE, True),        # 2x
    (17700.0, spx.WITHIN_RANGE, True),        # 3x - the upper bound, inclusive
    (17700.01, spx.EXTREME_CHANGE, True),     # just above 3x
    (29500.0, spx.EXTREME_CHANGE, True),      # 5x
    (63000.0, spx.EXTREME_CHANGE, True),      # the classic OCR misread of 6,300
    (2949.99, spx.EXTREME_CHANGE, True),      # just below 0.5x
])
def test_plausibility_band(shop, price, plausibility, material):
    row = wire_row(review(shop, (WIRE_ROW, price)))
    c = row.comparison.as_dict()
    assert c["plausibility"] == plausibility
    assert c["materialChange"] is material
    assert c["plausibleRange"] == [spx.PLAUSIBLE_MIN_RATIO, spx.PLAUSIBLE_MAX_RATIO]
    if plausibility == spx.EXTREME_CHANGE:
        assert row.reviewState == spx.STATE_REVIEW_REQUIRED


@pytest.mark.parametrize("price", [0, -6300, float("nan"), float("inf"),
                                   -float("inf"), 1_000_000.01, 1e12, "6300",
                                   "N/A", None, True])
def test_an_unreadable_price_is_excluded_not_compared(shop, price):
    result = review(shop, (WIRE_ROW, price), ("Anchor 1-Way 10A switch white", 81.0))
    assert all(r.skuId != WIRE for r in result.results)
    assert len(result.excluded) == 1
    assert result.excluded[0]["reason"] == "INVALID_ROW"


def test_the_ceiling_itself_is_still_a_price(shop):
    row = spx.build_supplier_line({"description": "x", "price": 1_000_000})
    assert row.price == 1_000_000
    with pytest.raises(spx.InvalidSupplierLineError):
        spx.build_supplier_line({"description": "x", "price": math.nan})


def test_conflicting_rows_still_decide_nothing(shop):
    result = review(shop, (WIRE_ROW, 6300), (WIRE_ROW, 63000))
    rows = [r for r in result.results if r.skuId == WIRE]
    assert {r.status for r in rows} == {spx.CONFLICT}
    assert all(r.comparison is None for r in rows)


def test_a_first_price_has_no_plausibility_judgement(shop):
    c = spx.PriceComparison(skuId=WIRE, previousPrice=None, currentPrice=6300.0,
                            threshold=5.0)
    assert c.plausibility == spx.NO_PREVIOUS_PRICE


def _price_list_payload(shop, price):
    return {"supplierName": "Sri Balaji Electricals", "lines": [
        r.as_dict() for r in review(shop, (WIRE_ROW, price)).results]}


def test_an_extreme_change_raises_no_alert_at_read_time(worker, monkeypatch, shop):  # noqa: F811
    seen = []
    monkeypatch.setattr(worker, "evaluate_price_change",
                        lambda *a, **k: seen.append(a) or {"alert": False})
    worker._publish_price_alerts("a" * 32, _price_list_payload(shop, 63000))
    assert seen == []
    worker._publish_price_alerts("a" * 32, _price_list_payload(shop, 6300))
    assert len(seen) == 1 and seen[0][3] == 6300.0


def test_the_page_says_to_check_the_document():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "frontend" / "site" /
            "index.html").read_text(encoding="utf-8")
    assert 'c.plausibility === "EXTREME_CHANGE"' in page
    assert "check the document before confirming" in page


# ---------------------------------------------------------------------------
# 2. ratings spoken in words
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("2 Havells MCB 32 amp C curve", "2 Havells MCB 32A C curve"),
    ("2 Havells MCB thirty two amp C curve", "2 Havells MCB 32A C curve"),
    ("2 Havells MCB thirty-two amps C curve", "2 Havells MCB 32A C curve"),
    ("2 Havells MCB 32A C curve", "2 Havells MCB 32A C curve"),
    ("2 Havells MCB 32 AC curve", "2 Havells MCB 32A C curve"),
    ("sixteen ampere isolator", "16A isolator"),
    ("two 32 amp MCB", "two 32A MCB"),             # the count stays a word
    ("thirty two MCB", "thirty two MCB"),          # no amp: not a rating
    ("one hundred amp", "one hundred amp"),        # beyond 1-99: left alone
    ("32 kg cable", "32 kg cable"),
    ("campaign amp", "campaign amp"),
])
def test_ratings_in_words_are_read_only_before_amp(text, expected):
    assert normalize_ratings(text)[0] == expected


def test_a_rating_in_words_quotes_the_named_breaker(shop):
    r = resolve_product(shop, requested_text="2 Havells MCB SP thirty two amp C curve")
    assert (r.status, r.skuId) == (RESOLVED, MCB)


def test_single_pole_spoken_is_sp_typed_asks(shop):
    spoken, notes = normalize_transcript("2 havels MCB single pole thirty two amp C curve")
    assert spoken == "2 Havells MCB SP 32A C curve"
    assert "thirty two amp -> 32A" in notes
    # Typed, "single pole" is not rewritten: the matcher asks SP or DP rather
    # than choosing - a question, never a guess.
    typed = resolve_product(shop, requested_text="2 Havells MCB single pole 32 amp C curve")
    assert typed.status == AMBIGUOUS
    assert {o["skuId"] for o in typed.as_dict()["options"]} == {
        MCB, "MCB-HAV-DP-32A-C"}


def test_tanglish_around_a_word_rating(shop):
    assert normalize_ratings("2 Havells MCB thirty two amp venum")[0] == \
        "2 Havells MCB 32A venum"


# ---------------------------------------------------------------------------
# 3. the API boundary
# ---------------------------------------------------------------------------

@pytest.fixture
def api_env(monkeypatch):
    table, queue = FakeTable(), FakeQueue()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv("ORDERS_QUEUE_URL", "https://sqs.test/q")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "sqs_client", lambda: queue)
    return table, queue


def call(route, body=None, headers=None, path=None):
    return api.handler({"routeKey": route, "headers": headers or {},
                        "body": json.dumps(body) if body is not None else None,
                        "pathParameters": path or {}}, None)


@pytest.mark.parametrize("route", sorted(api.OWNER_ROUTES))
def test_every_owner_route_refuses_an_anonymous_caller(api_env, route):
    response = call(route, {})
    assert response["statusCode"] == 401
    body = json.loads(response["body"])
    assert body["isAuthentication"] is False and body["demoGate"] is True


@pytest.mark.parametrize("value", ["", "owner", "DEMO-WORKSPACE", "demo-workspace "
                                   "extra", "true"])
def test_a_wrong_gate_value_is_refused(api_env, value):
    assert call("GET /api/customers",
                headers={api.DEMO_OWNER_HEADER: value})["statusCode"] == 401


def test_anonymous_demo_carries_no_stock(api_env):
    body = json.loads(call("GET /api/demo")["body"])
    text = json.dumps(body)
    assert "onHand" not in text and "inventory" not in body


@pytest.mark.parametrize("job_id,status", [
    ("../../etc/passwd", 400), ("A" * 32, 400), ("a" * 31, 400), ("a" * 33, 400),
    ("g" * 32, 400), ("' OR 1=1 --", 400), ("0" * 32, 404), ("f" * 32, 404),
])
def test_job_ids_cannot_be_walked(api_env, job_id, status):
    response = call("GET /api/jobs/{jobId}", path={"jobId": job_id})
    assert response["statusCode"] == status
    assert "orderText" not in response["body"]


def test_there_is_no_route_that_lists_jobs_anonymously():
    listing = [r for r in api.ROUTES if r.startswith("GET") and "jobs" in r]
    assert listing == ["GET /api/jobs/{jobId}"]
    assert "GET /api/intelligence" in api.OWNER_ROUTES


def test_a_customer_poll_of_an_order_carries_no_owner_data(api_env):
    table, _q = api_env
    job = "b" * 32
    quote = calculate_quote(cached_dataset(), [{"skuId": WIRE, "quantity": 3}]).as_dict()
    table.put_item(Item={
        "PK": f"JOB#{job}", "SK": "META", "jobId": job, "jobType": "ORDER",
        "status": "DONE", "orderText": "3 Finolex", "createdAt": 1,
        "customerId": "CUST-RAVI-001",
        "result": json.dumps({"status": "QUOTED", "quote": quote,
                              "marginProtection": {"affected": [{"x": 1}]},
                              "credit": {"creditLimit": 15000},
                              "modelId": "apac.amazon.nova-pro-v1:0"})})
    text = call("GET /api/jobs/{jobId}", path={"jobId": job})["body"]
    for leak in ("onHand", "costPrice", "marginProtection", "creditLimit",
                 "CUST-RAVI", "nova", "weeklyVelocity", "shortageQty"):
        assert leak not in text, leak


# ---------------------------------------------------------------------------
# 4. the figures every demo depends on
# ---------------------------------------------------------------------------

def test_canonical_quote_gst_and_inventory(shop):
    quote = calculate_quote(shop, [{"skuId": SWITCH, "quantity": 20},
                                   {"skuId": WIRE, "quantity": 3},
                                   {"skuId": MCB, "quantity": 2}]).as_dict()
    tax = gst.quote_gst(shop, quote)
    assert (quote["total"], tax["totalGst"], tax["grandTotal"]) == (
        22306.48, 4015.16, 26321.64)
    assert {(l["skuId"], l["onHand"], l["shortageQty"]) for l in quote["lines"]} == {
        (SWITCH, 14, 6), (WIRE, 1, 2), (MCB, 5, 0)}


def test_margin_and_walk_away(shop):
    m = margin_view(shop, WIRE, 6300.0)
    assert (m["oldMarginAmount"], m["newMarginAmount"],
            m["oldMarginPercent"], m["newMarginPercent"]) == (708.0, 308.0, 10.71, 4.66)
    assert sr.reply_context(shop, WIRE, {WIRE: 6300.0})["walkAwayPrice"] == 5947.2


@pytest.mark.parametrize("budget,funded,remaining", [
    (12947.99, False, 6299.99), (12948.00, True, 0.0), (12948.01, True, 0.01)])
def test_planner_commitment_boundary(shop, budget, funded, remaining):
    plan = build_purchase_plan(apply_confirmed_costs(shop, {WIRE: 6300.0}), budget)
    assert (plan["allCommitmentsFunded"], plan["remaining"]) == (funded, remaining)
    assert plan["totalSpend"] <= budget


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf")])
def test_planner_refuses_an_impossible_budget(shop, budget):
    with pytest.raises(InvalidBudgetError):
        build_purchase_plan(shop, budget)


# ---------------------------------------------------------------------------
# 5. what the deterministic path does with no model at all
# ---------------------------------------------------------------------------
# An independent evaluation asked how much of the flagship flow survives with
# the model removed. Pinned here, so the README's answer is evidence, not
# opinion: the quantity guard's line parser plus the catalogue matcher -
# neither of which calls a model - quote well-formed orders on their own, and
# turn an order they cannot resolve into a question rather than a guess. The
# model's job is the rest: orders that are not neatly one product per line.

from agent.quantity_guard import order_lines  # noqa: E402


def _no_model(shop, text):
    items, unresolved = [], 0
    for line in order_lines(text):
        match = resolve_product(shop, requested_text=line.get("text") or "")
        if match.status == RESOLVED and isinstance(line.get("quantity"), int):
            items.append({"skuId": match.skuId, "quantity": line["quantity"]})
        elif line.get("quantity") is not None:
            unresolved += 1
    return items, unresolved


@pytest.mark.parametrize("text", [
    "20 anchor 1way 10a switch white, finolex 1.5 red 90m 3 coil, havells sp 32a c mcb 2",
    "need twenty anchor 1 way switches white 10A and three finolex red 1.5 90m coils "
    "plus two havells 32A SP C curve MCBs",
])
def test_no_model_baseline_quotes_well_formed_orders(shop, text):
    items, unresolved = _no_model(shop, text)
    assert unresolved == 0
    assert calculate_quote(shop, items).as_dict()["total"] == 22306.48


def test_no_model_baseline_asks_rather_than_guesses(shop):
    items, unresolved = _no_model(
        shop, "Anna 20 Anchor switch 10 amp venum, 3 Finolex red 1.5 coil 90m, "
              "2 Havells MCB SP 32A")
    assert unresolved == 1          # the switch line is ambiguous: a question
    assert len(items) == 2
