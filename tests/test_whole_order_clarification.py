"""A clarification whose requestedText is the whole order.

The live failure this pins (4 of 34 stored DEMO sequences): Nova Pro searched
each line of "20 Anchor modular switch 1 way white, 3 Finolex 1.5 red coil,
2 Havells MCB 32 amp C curve" correctly, then called request_clarification
with the WHOLE order as requestedText. The question offered six products of
two brands; the answer, MCB-HAV-SP-32A-C, was stored against the whole order;
the quantity guard then read that SKU as sharing every word of every line,
all three lines tied, and the final run asked for a quantity with no options.

Now a requestedText stating more than one count is not trusted: the choice is
bound to the one order line whose matcher offers the chosen SKU, and when no
single line does, the question is asked again rather than guessed.
"""

from __future__ import annotations

import json

import pytest

import lambdas.api.handler as api
from agent import quantity_guard as qg
from engine import supplier_reply as sr
from engine.loader import cached_dataset
from engine.margin import margin_view
from test_confirmed_choice import (DEMO, MCB, MCB_DP, MCB_LINE, SWITCH,
                                   SWITCH_LINE, WIRE, WIRE_LINE, Shop, ask,
                                   option_for, prose)
from test_quantity_integrity import quote_call, turn
from test_queue import worker  # noqa: F401

HAVELLS_SWITCH = "SW-HAV-1W10A"
# What the guard's line splitter makes of the customer's MCB line.
MCB_ORDER_LINE = "2 Havells MCB 32A C curve"


@pytest.fixture
def shop(worker, monkeypatch):  # noqa: F811
    return Shop(worker, monkeypatch)


def ask_whole(shop, text, body=None):
    """The live model: searches the MCB line, then asks about the whole order."""
    accepted, row = shop.order(
        body or {"orderText": text},
        turn(("search_catalog", {"requestedText": MCB_LINE})),
        turn(("request_clarification", {
            "requestedText": text, "clarifyingAttribute": "specification",
            "question": "Please specify the switch and the MCB."})))
    result = shop.result(row)
    assert result["status"] == "NEEDS_CLARIFICATION"
    return accepted["jobId"], result["clarification"]


def answer(shop, text, job, clarification, sku, *turns):
    return shop.order({"orderText": text, "choice": {
        "jobId": job, "option": option_for(clarification, sku)}}, *turns)


# ---------------------------------------------------------------------------
# the binding itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sku,line", [
    (MCB, MCB_ORDER_LINE), (MCB_DP, MCB_ORDER_LINE),
    (WIRE, WIRE_LINE), (SWITCH, SWITCH_LINE)])
def test_a_whole_order_binds_each_sku_to_its_own_line(sku, line):
    assert api._answered_line(cached_dataset(), DEMO, DEMO, sku) == line


@pytest.mark.parametrize("requested", [
    MCB_LINE, "Havells MCB 32 amp C curve", "3 coils Finolex 1.5 sq mm wire", ""])
def test_one_line_is_kept_exactly_as_before(requested):
    assert api._answered_line(cached_dataset(), DEMO, requested, MCB) == requested


@pytest.mark.parametrize("text,sku", [
    (DEMO, HAVELLS_SWITCH),                     # offered, but no line names it
    (DEMO, "MCB-HAV-SP-6A-C"),                  # a real SKU of no line
    (f"{MCB_LINE}, 4 Havells MCB 32A C curve", MCB),   # two lines offer it
])
def test_no_single_line_is_no_binding(text, sku):
    assert api._answered_line(cached_dataset(), text, text, sku) is None


# ---------------------------------------------------------------------------
# A. the live failure, end to end
# ---------------------------------------------------------------------------

def test_a_the_live_sequence_now_ends_in_the_canonical_quotation(shop):
    job, q1 = ask_whole(shop, DEMO)
    # The question the live model produced: two brands, six products.
    assert q1["requestedText"] == DEMO
    assert {MCB, MCB_DP, SWITCH, HAVELLS_SWITCH} <= {o["skuId"] for o in q1["options"]}

    a2, row = answer(shop, DEMO, job, q1, MCB,
                     turn(("search_catalog", {"requestedText": WIRE_LINE})), prose())
    assert row["confirmed"] == [{"requestedText": MCB_ORDER_LINE, "skuId": MCB}]
    q2 = shop.result(row)["clarification"]
    a3, row = answer(shop, DEMO, a2["jobId"], q2, WIRE,
                     turn(("search_catalog", {"requestedText": SWITCH_LINE})), prose())
    q3 = shop.result(row)["clarification"]
    _a4, row = answer(shop, DEMO, a3["jobId"], q3, SWITCH,
                      turn(quote_call((SWITCH, 20), (WIRE, 3), (MCB, 2))))

    assert row["confirmed"] == [
        {"requestedText": MCB_ORDER_LINE, "skuId": MCB},
        {"requestedText": WIRE_LINE, "skuId": WIRE},
        {"requestedText": SWITCH_LINE, "skuId": SWITCH}]
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    quote = result["quote"]
    assert (quote["total"], quote["gst"]["totalGst"], quote["gst"]["grandTotal"]) == (
        22306.48, 4015.16, 26321.64)
    assert [r["status"] for r in qg.check_quantities(
        cached_dataset(), DEMO, result["matches"], quote)] == ["VERIFIED"] * 3


def test_a_the_model_searching_the_answered_line_again_still_gets_the_answer(shop):
    """The bound line is the splitter's "32A"; the model searches "32 amp"."""
    wire_90 = "3 Finolex 1.5 red 90m coil"
    text = f"{MCB_LINE}, {wire_90}"
    job, q1 = ask_whole(shop, text)
    _a, row = answer(shop, text, job, q1, MCB,
                     turn(("search_catalog", {"requestedText": MCB_LINE}),
                          ("search_catalog", {"requestedText": wire_90})),
                     turn(quote_call((MCB, 2), (WIRE, 3))))
    assert row["confirmed"] == [{"requestedText": MCB_ORDER_LINE, "skuId": MCB}]
    result = shop.result(row)
    assert result["status"] == "QUOTED", result.get("summary")
    assert [(l["skuId"], l["quantity"]) for l in result["quote"]["lines"]] == [
        (MCB, 2), (WIRE, 3)]


def test_a_the_old_binding_is_what_failed(shop):
    """Control: the same final quotation against the whole-order binding the
    API used to store is refused by the quantity guard, exactly as live."""
    data = cached_dataset()
    quote = {"lines": [{"skuId": SWITCH, "quantity": 20}, {"skuId": WIRE, "quantity": 3},
                       {"skuId": MCB, "quantity": 2}]}
    matches = [{"status": "RESOLVED", "skuId": MCB, "requestedText": DEMO},
               {"status": "RESOLVED", "skuId": WIRE, "requestedText": WIRE_LINE},
               {"status": "RESOLVED", "skuId": SWITCH, "requestedText": SWITCH_LINE}]
    assert {r["status"] for r in qg.check_quantities(data, DEMO, matches, quote)} == {
        qg.UNSTATED}
    matches[0]["requestedText"] = MCB_ORDER_LINE
    assert {r["status"] for r in qg.check_quantities(data, DEMO, matches, quote)} == {
        qg.VERIFIED}


# ---------------------------------------------------------------------------
# B. a question about one line is unchanged
# ---------------------------------------------------------------------------

def test_b_a_one_line_question_stores_the_models_words_as_before(shop):
    job, q1 = ask(shop, DEMO, MCB_LINE)
    assert q1["requestedText"] == MCB_LINE
    accepted, row = answer(shop, DEMO, job, q1, MCB,
                           turn(("search_catalog", {"requestedText": WIRE_LINE})), prose())
    assert accepted["choice"]["applied"] is True
    assert row["confirmed"] == [{"requestedText": MCB_LINE, "skuId": MCB}]


# ---------------------------------------------------------------------------
# C. no single line: nothing bound, asked again, nothing quoted
# ---------------------------------------------------------------------------

def test_c_a_sku_no_line_names_is_asked_again(shop):
    job, q1 = ask_whole(shop, DEMO)
    accepted, row = answer(shop, DEMO, job, q1, HAVELLS_SWITCH,
                           turn(("search_catalog", {"requestedText": SWITCH_LINE})), prose())
    assert accepted["choice"] == {"applied": False, "reason": "LINE_NOT_IDENTIFIED"}
    assert row["confirmed"] == []
    result = shop.result(row)
    assert result["status"] == "NEEDS_CLARIFICATION" and not result.get("quote")


def test_c_earlier_answers_stand_when_one_cannot_be_bound(shop):
    job, q1 = ask(shop, DEMO, WIRE_LINE)
    a2, row = shop.order({"orderText": DEMO, "choice": {
        "jobId": job, "option": option_for(q1, WIRE)}},
        turn(("search_catalog", {"requestedText": MCB_LINE})),
        turn(("request_clarification", {
            "requestedText": DEMO, "clarifyingAttribute": "specification",
            "question": "Which switch and MCB?"})))
    q2 = shop.result(row)["clarification"]
    accepted, row = answer(shop, DEMO, a2["jobId"], q2, HAVELLS_SWITCH,
                           turn(("search_catalog", {"requestedText": SWITCH_LINE})), prose())
    assert accepted["choice"]["applied"] is False
    assert row["confirmed"] == [{"requestedText": WIRE_LINE, "skuId": WIRE}]
    assert shop.result(row)["status"] == "NEEDS_CLARIFICATION"


def test_c_two_lines_offering_the_sku_are_asked_again(shop):
    text = f"{MCB_LINE}, 4 Havells MCB 32A C curve"
    job, q1 = ask_whole(shop, text)
    accepted, row = answer(shop, text, job, q1, MCB,
                           turn(("search_catalog", {"requestedText": MCB_LINE})), prose())
    assert accepted["choice"] == {"applied": False, "reason": "LINE_NOT_IDENTIFIED"}
    assert row["confirmed"] == []
    assert not shop.result(row).get("quote")


# ---------------------------------------------------------------------------
# D-F. the boundary around a whole-order question is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"clarifications": [{"requestedText": MCB_LINE, "skuId": MCB}]},
    {"skuId": MCB}, {"sku": MCB}, {"lines": [{"skuId": MCB, "quantity": 2}]},
    {"confirmed": [{"requestedText": MCB_LINE, "skuId": MCB}]},
    {"price": 1}, {"unitPrice": 1}, {"total": 1}, {"grandTotal": 1},
    {"gst": 0}, {"gstRate": 0}, {"taxMode": "EXEMPT"},
])
def test_d_e_nothing_but_the_option_is_accepted(shop, extra):
    job, q1 = ask_whole(shop, DEMO)
    response = shop.post({"orderText": DEMO, **extra, "choice": {
        "jobId": job, "option": option_for(q1, MCB)}})
    assert response["statusCode"] == 400
    assert not shop.queue.messages


@pytest.mark.parametrize("inner", [{"skuId": MCB}, {"price": 1}, {"requestedText": MCB_LINE}])
def test_d_a_choice_carries_nothing_but_job_and_option(shop, inner):
    job, q1 = ask_whole(shop, DEMO)
    response = shop.post({"orderText": DEMO, "choice": {
        "jobId": job, "option": option_for(q1, MCB), **inner}})
    assert response["statusCode"] == 400


def test_f_changed_text_stale_job_and_bad_options_are_still_refused(shop):
    job, q1 = ask_whole(shop, DEMO)
    option = option_for(q1, MCB)
    changed = shop.post({"orderText": DEMO + " urgent",
                         "choice": {"jobId": job, "option": option}})
    assert changed["statusCode"] == 400
    for bad in (0, len(q1["options"]) + 1, "1", True):
        assert shop.post({"orderText": DEMO, "choice": {
            "jobId": job, "option": bad}})["statusCode"] == 400
    shop.store.items[("JOB#" + job, "META")]["createdAt"] -= \
        api.CHOICE_MAX_AGE_SECONDS + 1
    stale = json.loads(shop.post({"orderText": DEMO, "choice": {
        "jobId": job, "option": option}})["body"])
    assert stale["choice"] == {"applied": False, "reason": "EXPIRED"}


# ---------------------------------------------------------------------------
# H. the supplier-cost figures do not move
# ---------------------------------------------------------------------------

def test_h_margin_and_walk_away_are_unchanged():
    data = cached_dataset()
    m = margin_view(data, WIRE, 6300.0)
    assert (m["oldMarginAmount"], m["newMarginAmount"],
            m["oldMarginPercent"], m["newMarginPercent"]) == (708.0, 308.0, 10.71, 4.66)
    assert sr.reply_context(data, WIRE, {WIRE: 6300.0})["walkAwayPrice"] == 5947.2
