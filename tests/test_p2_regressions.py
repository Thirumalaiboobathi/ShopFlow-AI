"""Regressions for the P2 issues an independent evaluation found on the live
deployment, after the P1 pass:

  1  +5.00% was a price alert that the price review called "not material".
  2  CloudWatch alarms published to the owner business-alert topic
     (asserted in tests/test_queue.py::test_8j / test_8j1 / test_8m).
  3  What-If read "increases by -Rs 500" as an increase of Rs 500.
  4  At Rs 12,947.99 the planner left a customer commitment one paisa short
     and spent Rs 6,293.56 on discretionary restock.
  5  Voice: Amazon Transcribe heard "Havells" as "hevels" and "Hels".
  6  The AI assistant page lost the workspace navigation and logout.
  7  The owner's quote showed the Bedrock model id.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

import lambdas.api.handler as api
from engine import whatif
from engine.loader import load_dataset
from engine.matching import RESOLVED, resolve_product
from engine.price_alerts import PERCENT_THRESHOLD, evaluate_price_change
from engine.pricing import (PRICE_ALERT_THRESHOLD_PERCENT, is_material_change,
                            percent_change)
from engine.supplier_prices import compare_price
from engine.voice import answer_shop_query, brand_vocabulary, correct_brand_terms
from test_api import FakeTable

SWITCH_2WAY = "SW-ANC-2W10A"      # last paid Rs 92.00
WIRE = "W-FIN-1.5-RED-90M"
MCB = "MCB-HAV-SP-32A-C"
OWNER = {"x-shopflow-demo-owner": "demo-workspace"}
PAGE = (Path(__file__).resolve().parents[1] / "frontend" / "site" /
        "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shop():
    return load_dataset()


# ---------------------------------------------------------------------------
# 1. one material-change threshold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("new,pct,material", [
    (96.59, 4.99, False),     # +4.99%
    (96.60, 5.00, True),      # exactly +5.00% - the live contradiction
    (96.61, 5.01, True),      # +5.01%
])
def test_the_review_and_the_alert_agree_at_the_boundary(shop, new, pct,
                                                         material):
    review = compare_price(shop, SWITCH_2WAY, new)
    alert = evaluate_price_change(shop, SWITCH_2WAY, 92.0, new,
                                  confirmed_costs={}, now=1)
    assert review.percentageDelta == pct
    assert review.materialChange is material
    assert (PERCENT_THRESHOLD in alert["triggers"]) is material
    assert alert["materialChange"] is material


def test_a_decrease_is_material_but_never_a_price_shock_alert(shop):
    review = compare_price(shop, SWITCH_2WAY, 80.0)        # -13.04%
    assert (review.direction, review.materialChange) == ("DECREASE", True)
    alert = evaluate_price_change(shop, SWITCH_2WAY, 92.0, 80.0,
                                  confirmed_costs={}, now=1)
    assert alert["alert"] is False and alert["triggers"] == []


def test_an_unchanged_price_is_neither(shop):
    review = compare_price(shop, SWITCH_2WAY, 92.0)
    assert (review.direction, review.materialChange) == ("UNCHANGED", False)
    alert = evaluate_price_change(shop, SWITCH_2WAY, 92.0, 92.0,
                                  confirmed_costs={}, now=1)
    assert alert["alert"] is False


def test_no_increase_is_ever_both_material_and_not(shop):
    """Every increase from +0.01% to +12%, on three SKUs at three price
    levels: the review's verdict and the alert's percentage trigger are the
    same function, so they can never disagree."""
    for sku, old in ((SWITCH_2WAY, 92.0), (MCB, 358.0), (WIRE, 5900.0)):
        base = Decimal(str(old))
        for bp in range(1, 1201, 7):                  # basis points
            new = float((base * (10000 + bp) / 10000).quantize(Decimal("0.01")))
            if new <= old:
                continue
            verdict = is_material_change(old, new)
            alert = evaluate_price_change(shop, sku, old, new,
                                          confirmed_costs={}, now=1)
            assert (PERCENT_THRESHOLD in alert["triggers"]) is verdict, (sku, new)
            assert alert["materialChange"] is verdict


def test_the_percentage_is_decimal_half_up():
    """No float boundary: 100 -> 105 is exactly 5.00, and 3 -> 3.15 too."""
    assert percent_change(100, 105) == Decimal("5.00")
    assert percent_change(3, 3.15) == Decimal("5.00")
    assert percent_change(92, 96.6) == Decimal("5.00")
    assert is_material_change(3, 3.15) is True
    assert PRICE_ALERT_THRESHOLD_PERCENT == 5.0


# ---------------------------------------------------------------------------
# 3. What-If: a signed amount is a question
# ---------------------------------------------------------------------------

def _wi(shop, question):
    return whatif.simulate(shop, question, confirmed_costs={WIRE: 6300.0},
                           sku_id=WIRE)


@pytest.mark.parametrize("question", [
    "What if Finolex wire price increases by -₹500?",
    "What if supplier cost increase by negative ₹500?",
    "What if supplier cost increase by -500?",
    "What if I add negative 500 to the supplier cost?",
    "What if supplier price increase -₹500?",
    "What if supplier cost increases by ₹-500?",
])
def test_a_negative_increase_is_asked_not_simulated(shop, question):
    r = _wi(shop, question)
    assert r["status"] == "NEEDS_CLARIFICATION"
    assert r["explanation"] == ("The increase amount cannot be negative. Did "
                                "you mean an increase of ₹500 or a decrease "
                                "of ₹500?")
    assert [o["value"] for o in r["clarification"]["options"]] == [
        "INCREASE", "DECREASE"]
    assert r["stateChanged"] is False
    assert r["grounded"] is True
    assert "scenario" not in r


def test_a_negative_decrease_is_asked(shop):
    r = _wi(shop, "What if supplier cost decreases by -₹500?")
    assert r["status"] == "NEEDS_CLARIFICATION"
    assert r["explanation"].startswith("The decrease amount cannot be negative.")


def test_a_negative_percentage_is_asked(shop):
    r = _wi(shop, "What if supplier cost increases by -10%?")
    assert r["status"] == "NEEDS_CLARIFICATION"
    assert "10%" in r["explanation"]


@pytest.mark.parametrize("question,cost", [
    ("What if supplier cost decreases by ₹500?", 5800.0),
    ("What if supplier cost increases by ₹500?", 6800.0),
    ("What if Finolex wire price increases by ₹300?", 6600.0),
    ("What if W-FIN-1.5-RED-90M supplier price increases by ₹300?", 6600.0),
])
def test_ordinary_changes_still_simulate(shop, question, cost):
    r = _wi(shop, question)
    assert r["status"] == "SIMULATED"
    assert r["scenario"]["supplierCost"] == cost
    assert r["stateChanged"] is False


def test_a_zero_change_is_refused_like_zero_percent(shop):
    amount = _wi(shop, "What if supplier cost increases by ₹0?")
    percent = _wi(shop, "What if supplier cost increases by 0%?")
    assert amount["status"] == percent["status"] == "REFUSED"
    assert "changes nothing" in amount["explanation"]


def test_the_live_route_asks_and_writes_nothing(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(api, "table", lambda: table)
    response = api.handler({
        "routeKey": "POST /api/shop-queries", "headers": OWNER,
        "body": json.dumps({"kind": "WHAT_IF", "skuId": WIRE, "question":
                            "What if Finolex wire price increases by -₹500?"})},
        None)
    body = json.loads(response["body"])
    assert (response["statusCode"], body["status"]) == (200,
                                                        "NEEDS_CLARIFICATION")
    assert body["stateChanged"] is False
    assert table.items == {}


# ---------------------------------------------------------------------------
# 4. planner: commitments first, to the paisa
# ---------------------------------------------------------------------------

COMMITMENTS = 12948.0      # 6 Anchor x 58 + 2 Finolex x 6,300


def _plan(shop, budget):
    return whatif._plan(shop, float(budget), {WIRE: 6300.0})


def test_one_paisa_short_buys_no_restock(shop):
    p = _plan(shop, COMMITMENTS - 0.01)
    assert p["allCommitmentsFunded"] is False
    assert p["restockCost"] == 0.0 and p["restockSelected"] == []
    # The shortfall is not rounded away: the unspent money is still there.
    assert (p["commitmentCost"], p["remaining"]) == (6648.0, 6299.99)
    assert all("customer commitment is not fully funded" in r["reason"]
               for r in p["restockDeferred"])


@pytest.mark.parametrize("budget,remaining", [
    (COMMITMENTS, 0.0), (COMMITMENTS + 0.01, 0.01)])
def test_exact_and_one_paisa_over(shop, budget, remaining):
    p = _plan(shop, budget)
    assert p["allCommitmentsFunded"] is True
    assert p["commitmentCost"] == COMMITMENTS
    assert (p["restockCost"], p["remaining"]) == (0.0, remaining)


@pytest.mark.parametrize("budget,spend,remaining", [
    (0, 0.0, 0.0),
    (25000, 24993.16, 6.84),                 # the canonical plan, unchanged
    (10_000_000, 2870989.85, 7129010.15),
])
def test_other_budgets(shop, budget, spend, remaining):
    p = _plan(shop, budget)
    assert (p["totalSpend"], p["remaining"]) == (spend, remaining)


def test_no_budget_ever_restocks_while_a_commitment_is_short(shop):
    for paise in list(range(0, 1300000, 12347)) + [1294799, 1294800, 1294801]:
        p = _plan(shop, paise / 100)
        if not p["allCommitmentsFunded"]:
            assert p["restockCost"] == 0.0, paise
        assert p["totalSpend"] <= paise / 100 + 1e-9
        assert round(p["totalSpend"] + p["remaining"], 2) == round(paise / 100, 2)


# ---------------------------------------------------------------------------
# 5. voice: a misheard brand, corrected only when it can only be one brand
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vocab(shop):
    return brand_vocabulary(shop)


@pytest.mark.parametrize("heard,expected", [
    ("2 Havells MCB SP 32A C-curve", "2 Havells MCB SP 32A C-curve"),
    ("2 hevels MCB SP 32A", "2 Havells MCB SP 32A"),
    ("2 Hels MCB SP 32 amp C curve", "2 Havells MCB SP 32 amp C curve"),
])
def test_havells_as_transcribe_heard_it(vocab, heard, expected):
    assert correct_brand_terms(heard, vocab)[0] == expected


@pytest.mark.parametrize("heard", [
    "Hels",                          # no product named: not corrected
    "2 hevels",                      # no product named
    "2 Siemens MCB",                 # a brand the shop does not stock
    "2 SKU-FAKE-99 hevels",          # a fake SKU, no product named
    "2 Havells fan and 3 Hels",      # "Hels" line names no product
])
def test_nothing_is_guessed(vocab, heard):
    assert correct_brand_terms(heard, vocab) == (heard, [])


def test_an_ambiguous_brand_is_left_as_heard():
    vocab = {"brands": {"Havells": {"mcb"}, "Hovells": {"mcb"}},
             "known": set()}
    assert correct_brand_terms("2 hevells MCB", vocab) == ("2 hevells MCB", [])


def test_a_catalogue_word_is_never_rewritten(vocab):
    assert correct_brand_terms("20 anchor switch", vocab) == (
        "20 anchor switch", [])


def test_the_voice_route_shows_the_correction_and_the_matcher_resolves(shop):
    r = answer_shop_query(shop, "2 Hels MCB SP 32 amp C curve venum")
    assert r["delegateTo"] == "ORDER"
    assert r["normalizedTranscript"].startswith("2 Havells MCB SP 32")
    assert "Hels -> Havells" in r["aliasesApplied"]
    # As the order agent searches: the brand it now reads resolves the SKU;
    # the brand as heard would have matched nothing.
    corrected = resolve_product(shop, requested_text=r["normalizedTranscript"],
                                brand="Havells", category="MCB",
                                specification="SP 32A")
    assert (corrected.status, corrected.skuId) == (RESOLVED, MCB)
    heard = resolve_product(shop, requested_text="2 Hels MCB SP 32 amp",
                            brand="Hels", category="MCB",
                            specification="SP 32A")
    assert heard.skuId is None


# ---------------------------------------------------------------------------
# 6 + 7. the page
# ---------------------------------------------------------------------------

def test_the_workspace_header_is_shared_with_the_assistant():
    chrome = PAGE.index('id="wsChrome"')
    assert chrome < PAGE.index('<div id="viewWorkspace"')
    assert chrome < PAGE.index('id="viewAssistant"')
    header = PAGE[chrome:PAGE.index('<div id="viewWorkspace"')]
    assert 'id="logoutBtn"' in header and 'id="navAssistant"' in header
    assert header.count('class="wslink"') == 6
    # One header, not a copy per view.
    assert PAGE.count('id="logoutBtn"') == 1
    assert 'el("wsChrome").hidden = id === "viewLogin";' in PAGE


def test_the_quote_does_not_render_the_model_id():
    assert "result.modelId" not in PAGE
    assert not re.search(r"model:\s*'\s*\+", PAGE)
    assert "apac.amazon" not in PAGE and "nova-pro" not in PAGE
