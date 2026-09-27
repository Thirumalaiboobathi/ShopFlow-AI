"""Regressions for the four P1 defects an independent evaluation found on the
LIVE deployment, and the promise-keeping split in the daily brief.

  P1 #1  "2 Anchor modular switches 1-Way 10A White and 3 coils Finolex ..."
         was asked "1-Way 10A or 2-Way 10A?" three times out of three. The
         customer's count, 2, scored as a hit on "2-Way".
  P1 #2  "minus 2 Havells MCB SP 32A C-curve" was quoted as two breakers.
  P1 #3  A price list with the same SKU at Rs 358 and Rs 400 raised a price
         alert at 400; rows with unreadable rates vanished without a word.
  P1 #4  Per-order events were routed to the owner's SNS topic (asserted in
         tests/test_queue.py::test_8m and tests/test_business_events.py).

The exact live inputs are used verbatim. scripts/smoke_test_p1_regressions.py
sends the same inputs to the deployed application.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lambdas.api.handler as api
from agent.orchestrator import (STATUS_NEEDS_CLARIFICATION, STATUS_QUOTED,
                                run_order_agent)
from agent.quantity_guard import NEGATIVE, negative_lines, order_lines
from agent.textract_reader import (PRICE_MISSING, PRICE_NOT_A_NUMBER,
                                   PRICE_NOT_POSITIVE, extract_rows,
                                   read_price_list)
from agent.tools import NEEDS_CORRECTION, run_tool
from engine import brief as eb
from engine.loader import load_dataset
from engine.matching import AMBIGUOUS, NOT_FOUND, RESOLVED, resolve_product
from engine.supplier_prices import (CONFLICT, DUPLICATE, MATCHED,
                                    review_price_list)
from observability import events
from test_api import FakeTable
from test_price_alerts import AlertTable, Bus
from test_quantity_integrity import FakeBedrock, quote_call, turn
from test_queue import worker  # noqa: F401
from test_supplier_documents import FakeTextract, blocks_for

SWITCH = "SW-ANC-1W10A"
SWITCH_2WAY = "SW-ANC-2W10A"
WIRE = "W-FIN-1.5-RED-90M"
MCB = "MCB-HAV-SP-32A-C"
OWNER = {"x-shopflow-demo-owner": "demo-workspace"}
PAGE = (Path(__file__).resolve().parents[1] / "frontend" / "site" /
        "index.html").read_text(encoding="utf-8")

# The live inputs, verbatim.
TWO_LINE_AND = ("2 Anchor modular switches 1-Way 10A White and 3 coils "
                "Finolex 1.5 sq mm FR wire red 90m")
TWO_LINE_COMMA = ("2 Anchor modular switches 1-Way 10A White, 3 coils "
                  "Finolex 1.5 sq mm FR wire red 90m")
ANCHOR_LINE = "2 Anchor modular switches 1-Way 10A White"
WIRE_LINE = "3 coils Finolex 1.5 sq mm FR wire red 90m"
MCB_LINE = "2 Havells MCB SP 32A C-curve"
CANONICAL = ("20 Anchor modular switches 1-Way 10A White, 3 coils Finolex "
             "1.5 sq mm FR wire red 90m, 2 Havells MCB SP 32A C-curve")


@pytest.fixture(scope="module")
def shop():
    return load_dataset()


# ---------------------------------------------------------------------------
# P1 #1 - a quantity is never a product specification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    ANCHOR_LINE,
    "Anchor modular switches 1-Way 10A White",
    "20 Anchor modular switches 1-Way 10A White",
    "2 anchor modular switch 1 way 10a white",
    "2 anchor 1way 10a white switch",
])
def test_the_anchor_line_resolves_to_1_way_whatever_the_count(shop, text):
    """The exact requestedText the live model sent - quantity included."""
    match = resolve_product(shop, requested_text=text)
    assert (match.status, match.skuId) == (RESOLVED, SWITCH)


def test_2_way_is_still_2_way(shop):
    match = resolve_product(shop, requested_text=
                            "2 Anchor modular switches 2-Way 10A White")
    assert (match.status, match.skuId) == (RESOLVED, SWITCH_2WAY)


def test_an_unstated_rating_is_still_a_question(shop):
    match = resolve_product(shop, requested_text="2 Anchor switches")
    assert match.status == AMBIGUOUS
    assert match.clarifyingAttribute == "specification"


@pytest.mark.parametrize("count", [1, 2, 3, 4, 10, 20, 25])
def test_no_count_changes_what_any_product_matches(shop, count):
    """Across the whole catalogue: prefixing a count to a product's own name
    never changes the verdict. Before the fix, 2 turned 1-Way into a tie, and
    20 or 25 could pick a conduit by its SKU number."""
    for product in shop.products.values():
        plain = resolve_product(shop, requested_text=product.name)
        counted = resolve_product(shop, requested_text=f"{count} {product.name}")
        assert (counted.status, counted.skuId) == (plain.status, plain.skuId), \
            (count, product.skuId)


def test_a_count_alone_matches_nothing(shop):
    assert resolve_product(shop, requested_text="20").status == NOT_FOUND


def test_a_count_does_not_choose_a_size(shop):
    """"20 conduit" is twenty conduits of an unstated size, not 20mm."""
    match = resolve_product(shop, requested_text="20 conduit")
    assert match.status == AMBIGUOUS


@pytest.mark.parametrize("text,sku", [
    ("ACC-CONDUIT-20", "ACC-CONDUIT-20"),
    ("2 MCB-HAV-SP-32A-C", MCB),
    ("2 MCB_HAV_SP_32A_C", MCB),
])
def test_a_written_sku_id_still_resolves(shop, text, sku):
    match = resolve_product(shop, requested_text=text)
    assert (match.status, match.skuId) == (RESOLVED, sku)


@pytest.mark.parametrize("text", [TWO_LINE_AND, TWO_LINE_COMMA])
def test_one_search_cannot_carry_two_order_lines(shop, text):
    """Scored together, one line's words can pick the other line's product."""
    out = run_tool(shop, "search_catalog", {"requestedText": text})
    assert out["status"] == NEEDS_CORRECTION
    assert out["skuId"] is None


def test_one_line_with_commas_between_its_attributes_is_searched(shop):
    out = run_tool(shop, "search_catalog", {
        "requestedText": "3 coils Finolex 1.5 sq mm FR wire, red, 90m"})
    assert (out["status"], out["skuId"]) == (RESOLVED, WIRE)


def _two_line_run(shop, order, *lines):
    searches = [("search_catalog", {"requestedText": line}) for line, _ in lines]
    return run_order_agent(shop, order, client=FakeBedrock([
        turn(*searches),
        turn(quote_call(*[(sku, qty) for (_line, (sku, qty)) in lines])),
    ]))


@pytest.mark.parametrize("order", [TWO_LINE_AND, TWO_LINE_COMMA])
def test_the_live_two_line_order_is_quoted(shop, order):
    """The model searches each line with its count still in the text, as the
    live trace showed. Both lines resolve; the order is quoted."""
    result = _two_line_run(shop, order, (ANCHOR_LINE, (SWITCH, 2)),
                           (WIRE_LINE, (WIRE, 3)))
    assert result.status == STATUS_QUOTED
    assert {l["skuId"]: l["quantity"] for l in result.quote["lines"]} == {
        SWITCH: 2, WIRE: 3}
    assert result.quote["total"] == 19980.6    # 2 x 78.30 + 3 x 6,608


def test_anchor_alone_is_quoted(shop):
    result = _two_line_run(shop, ANCHOR_LINE, (ANCHOR_LINE, (SWITCH, 2)))
    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == 156.6


def test_anchor_and_havells_is_quoted(shop):
    order = f"{ANCHOR_LINE} and {MCB_LINE}"
    result = _two_line_run(shop, order, (ANCHOR_LINE, (SWITCH, 2)),
                           (MCB_LINE, (MCB, 2)))
    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == 1073.08


def test_the_canonical_order_is_still_22306_48(shop):
    result = _two_line_run(
        shop, CANONICAL,
        ("20 Anchor modular switches 1-Way 10A White", (SWITCH, 20)),
        (WIRE_LINE, (WIRE, 3)), (MCB_LINE, (MCB, 2)))
    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == 22306.48


# ---------------------------------------------------------------------------
# P1 #2 - a subtraction is never a positive quantity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "minus 2 Havells MCB SP 32A C-curve",
    "minus 3 Anchor modular switches 1-Way 10A White",
    "less 2 Havells MCB SP 32A C-curve",
    "less 3 Anchor modular switches 1-Way 10A White",
    "negative 2 Havells MCB SP 32A C-curve",
    "subtract 2 Havells MCB SP 32A C-curve",
    "deduct 2 Havells MCB SP 32A C-curve",
    "reduce by 2 Havells MCB SP 32A C-curve",
    "-2 Havells MCB SP 32A C-curve",
    "minus two Havells MCB SP 32A C-curve",
])
def test_a_signed_count_is_read_as_negative(text):
    [line] = order_lines(text)
    assert line["negative"] is True
    assert negative_lines(text) == [line]


@pytest.mark.parametrize("text", [
    "2 Havells MCB SP 32A C-curve",
    "Havells MCB SP 32A C-curve - 2 nos",   # a dash as a separator
    CANONICAL,
])
def test_an_ordinary_count_is_not_negative(text):
    assert negative_lines(text) == []


@pytest.mark.parametrize("order,sku,qty", [
    ("minus 2 Havells MCB SP 32A C-curve", MCB, 2),
    ("minus 3 Anchor modular switches 1-Way 10A White", SWITCH, 3),
    ("less 2 Havells MCB SP 32A C-curve", MCB, 2),
    ("less 3 Anchor modular switches 1-Way 10A White", SWITCH, 3),
    ("negative 2 Havells MCB SP 32A C-curve", MCB, 2),
    ("subtract 2 Havells MCB SP 32A C-curve", MCB, 2),
    ("-2 Havells MCB SP 32A C-curve", MCB, 2),
])
def test_a_signed_order_is_never_quoted_at_the_positive_count(shop, order,
                                                              sku, qty):
    """The live failure: the model quoted the positive count and every other
    check agreed. The quotation is withheld and the owner is asked."""
    result = run_order_agent(shop, order, client=FakeBedrock([
        turn(("search_catalog", {"requestedText": order})),
        turn(quote_call((sku, qty))),
    ]))
    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert result.clarification["clarifyingAttribute"] == "quantity"
    assert "negative quantity" in result.clarification["question"]
    assert result.quantityCheck[0]["verdict"] == NEGATIVE


def test_a_signed_line_withholds_the_whole_order(shop):
    order = f"{MCB_LINE}, minus 3 Anchor modular switches 1-Way 10A White"
    result = run_order_agent(shop, order, client=FakeBedrock([
        turn(("search_catalog", {"requestedText": MCB_LINE}),
             ("search_catalog", {"requestedText":
                                 "Anchor modular switches 1-Way 10A White"})),
        turn(quote_call((MCB, 2), (SWITCH, 3))),
    ]))
    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None


# ---------------------------------------------------------------------------
# P1 #3 - a price list that contradicts itself decides nothing
# ---------------------------------------------------------------------------

def _rows(*pairs):
    return [{"description": d, "price": p, "confidence": 99.0,
             "source": "TEXTRACT"} for d, p in pairs]


def test_the_same_sku_at_two_prices_is_a_conflict(shop):
    review = review_price_list(shop, "Sri Balaji", None, _rows(
        ("Havells MCB SP 32A C-Curve", 358.0),
        ("Havells MCB SP 32A C-Curve", 400.0)))
    payload = review.as_dict()
    assert [l["status"] for l in payload["lines"]] == [CONFLICT, CONFLICT]
    assert all(l["comparison"] is None for l in payload["lines"])
    assert payload["lines"][0]["conflictingPrices"] == [358.0, 400.0]
    assert payload["conflictCount"] == 1
    assert payload["materialChangeCount"] == 0
    assert "at different prices" in payload["conflictNotice"]


def test_three_conflicting_rows_are_all_conflicts(shop):
    review = review_price_list(shop, "S", None, _rows(
        ("Havells MCB SP 32A C-Curve", 400.0),
        ("Havells MCB SP 32A C-Curve", 358.0),
        ("Havells MCB SP 32A C-Curve", 420.0),
        ("Havells MCB SP 16A C-Curve", 350.0)))
    statuses = [r.status for r in review.results]
    assert statuses == [CONFLICT, CONFLICT, CONFLICT, MATCHED]
    assert review.results[0].conflictingPrices == [358.0, 400.0, 420.0]


def test_the_same_sku_at_the_same_price_is_judged_once(shop):
    review = review_price_list(shop, "S", None, _rows(
        ("Havells MCB SP 32A C-Curve", 400.0),
        ("Havells MCB SP 32A C-Curve", 400.0)))
    assert [r.status for r in review.results] == [MATCHED, DUPLICATE]
    assert review.results[0].comparison is not None
    assert review.results[1].comparison is None
    assert len(review.materialChanges) == 1


@pytest.fixture
def wired(worker, monkeypatch):  # noqa: F811
    bus = Bus()
    monkeypatch.setattr(worker, "_table", AlertTable({}))
    monkeypatch.setenv("EVENT_BUS_NAME", "shopflow-business-events")
    monkeypatch.setattr(events, "_client", bus)
    return worker, bus


def test_a_conflict_raises_no_alert(shop, wired):
    """The live document: 358 and 400 for the same MCB alerted at 400."""
    worker, bus = wired
    payload = review_price_list(shop, "Sri Balaji", None, _rows(
        ("Havells MCB SP 32A C-Curve", 358.0),
        ("Havells MCB SP 32A C-Curve", 400.0),
        ("Finolex 1.5 sqmm FR Wire 90m coil", 7000.0),        # ambiguous
        ("Siemens Contactor 3TF 40A", 2500.0))).as_dict()     # unknown
    assert worker._publish_price_alerts("a" * 32, payload) == []
    assert bus.entries == []


def test_a_duplicate_at_the_same_price_alerts_once(shop, wired):
    worker, bus = wired
    payload = review_price_list(shop, "Sri Balaji", None, _rows(
        ("Havells MCB SP 16A C-Curve", 350.0),
        ("Havells MCB SP 16A C-Curve", 350.0))).as_dict()
    sent = worker._publish_price_alerts("a" * 32, payload)
    assert [a["skuId"] for a in sent] == ["MCB-HAV-SP-16A-C"]
    assert len(bus.entries) == 1


def test_a_conflicting_price_cannot_be_confirmed(shop, monkeypatch):
    table = FakeTable()
    job_id = "c" * 32
    payload = review_price_list(shop, "S", None, _rows(
        ("Havells MCB SP 32A C-Curve", 358.0),
        ("Havells MCB SP 32A C-Curve", 400.0))).as_dict()
    table.put_item({**api._job_key(job_id), "jobId": job_id,
                    "result": json.dumps({"review": payload})})
    monkeypatch.setattr(api, "table", lambda: table)
    response = api.handler({
        "routeKey": "POST /api/price-decisions", "headers": OWNER,
        "body": json.dumps({"jobId": job_id, "skuId": MCB,
                            "decision": "CONFIRMED"})}, None)
    assert response["statusCode"] == 409
    assert json.loads(response["body"])["status"] == "CONFLICT"
    assert not [k for k in table.items if "COST#" in str(k)]


UNREADABLE = [
    ("Havells MCB SP 32A C-Curve", "358.00", 99.0),
    ("Anchor Modular Switch Bell Push White", "N/A", 99.0),
    ("Anchor Modular Switch Socket 6A White", "", 99.0),
    ("Anchor Modular Switch Socket 16A White", "-148.00", 99.0),
]


def test_unreadable_rows_are_reported_not_dropped():
    rows, excluded, _supplier, _date = extract_rows(blocks_for(UNREADABLE))
    assert [r["description"] for r in rows] == ["Havells MCB SP 32A C-Curve"]
    assert [(e["description"], e["reason"]) for e in excluded] == [
        ("Anchor Modular Switch Bell Push White", PRICE_NOT_A_NUMBER),
        ("Anchor Modular Switch Socket 6A White", PRICE_MISSING),
        ("Anchor Modular Switch Socket 16A White", PRICE_NOT_POSITIVE),
    ]


def test_the_review_says_how_many_rows_were_excluded(shop):
    rows, _s, _d, excluded = read_price_list(
        b"x", client=FakeTextract(blocks_for(UNREADABLE)), with_excluded=True)
    payload = review_price_list(shop, "S", None, rows,
                                excluded_rows=excluded).as_dict()
    assert payload["excludedCount"] == 3
    assert payload["exclusionNotice"] == (
        "3 rows could not be interpreted and were excluded from price "
        "analysis.")
    assert [r["description"] for r in payload["excludedRows"]] == [
        d for d, _p, _c in UNREADABLE[1:]]


def test_a_bad_model_row_is_excluded_not_fatal(shop):
    """The model reader's rows reach the review unparsed. One bad row used to
    fail the whole document; now it is reported and the rest is reviewed."""
    payload = review_price_list(shop, "S", None, [
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0},
        {"description": "Anchor Modular Switch Bell Push White", "price": None},
    ]).as_dict()
    assert payload["lineCount"] == 1
    assert payload["excludedCount"] == 1
    assert payload["exclusionNotice"].startswith("1 row could not be")


def test_the_owner_sees_conflicts_and_exclusions():
    assert "review.exclusionNotice" in PAGE
    assert "review.conflictNotice" in PAGE
    assert 'l.status === "CONFLICT"' in PAGE


# ---------------------------------------------------------------------------
# Promise-keeping cost - commitments apart from discretionary restock
# ---------------------------------------------------------------------------

NOW = 1790500000
CONFIRMED = {WIRE: 6300.0}


def test_commitments_and_discretionary_restock_are_named_apart(shop):
    b = eb.build_brief(shop, CONFIRMED, budget=25000, now=NOW)
    pk = b["promiseKeeping"]
    wire = next(c for c in pk["commitments"] if c["skuId"] == WIRE)
    assert (wire["quantity"], wire["unitCost"], wire["cost"], wire["funded"]) \
        == (2, 6300.0, 12600.0, True)
    assert (wire["walkAwayPrice"], wire["aboveWalkAwayCost"]) == (5947.2, 705.6)
    assert pk["commitmentCost"] == b["planner"]["commitmentCost"] == 12948.0
    [extra] = pk["discretionary"]
    assert (extra["skuId"], extra["decision"], extra["currentCost"],
            extra["walkAwayPrice"], extra["aboveBy"]) == \
        (WIRE, "DO_NOT_BUY", 6300.0, 5947.2, 352.8)
    assert extra["text"] == (
        "Do not add discretionary restock of Finolex 1.5 sqmm FR Wire Red 90m "
        "coil at ₹6,300.00 because it exceeds the ₹5,947.20 walk-away price.")
    assert b["grounded"] is True


def test_every_promise_keeping_figure_is_the_planners(shop):
    from engine.whatif import _plan
    plan = _plan(shop, 25000.0, CONFIRMED)
    b = eb.build_brief(shop, CONFIRMED, budget=25000, now=NOW)
    by_sku = {c["skuId"]: c for c in plan["commitments"]}
    for c in b["promiseKeeping"]["commitments"]:
        source = by_sku[c["skuId"]]
        assert (c["quantity"], c["unitCost"], c["cost"]) == (
            source["requestedQty"], source["unitCost"], source["fullLineCost"])
    restock = {r["skuId"]: r for r in
               plan["restockSelected"] + plan["restockDeferred"]}
    [extra] = b["promiseKeeping"]["discretionary"]
    assert extra["recommendedQty"] == restock[WIRE]["requestedQty"]


def test_an_unfunded_commitment_says_so(shop):
    b = eb.build_brief(shop, CONFIRMED, budget=5000, now=NOW)
    assert any(not c["funded"] and "not funded" in c["text"]
               for c in b["promiseKeeping"]["commitments"])
    assert b["grounded"] is True


def test_a_cost_within_the_walk_away_price_is_not_refused(shop):
    """Anchor 58 -> 62 is an alert (+6.9%) but under its 70.47 walk-away."""
    b = eb.build_brief(shop, {SWITCH: 62.0}, budget=25000, now=NOW)
    [extra] = b["promiseKeeping"]["discretionary"]
    assert extra["decision"] != "DO_NOT_BUY"
    assert not any(a["kind"] == "RENEGOTIATE_OR_REPRICE"
                   for a in b["priorityActions"])
    assert b["grounded"] is True


def test_the_page_shows_the_promise_keeping_split():
    assert "Promise-keeping cost" in PAGE
    assert "required to fulfil existing commitments" in PAGE
    assert "Discretionary restock" in PAGE
