"""The canonical demo scenario.

These tests guard the story the demo tells. If a seed change breaks the
tradeoff, or makes a narrated number drift, the suite fails rather than the
demo quietly becoming dishonest.
"""

from __future__ import annotations

import pytest

from data.demo_scenario import DEMO_BUDGET, assert_budget_binds, build_scenario


@pytest.fixture(scope="module")
def scenario():
    return build_scenario()


def test_demo_order_has_the_three_narrated_lines(scenario):
    skus = {l["skuId"] for l in scenario["order"]["lines"]}
    assert skus == {"SW-ANC-1W10A", "W-FIN-1.5-RED-90M", "MCB-HAV-SP-32A-C"}


def test_quote_total_is_the_sum_of_its_lines(scenario):
    lines = scenario["order"]["lines"]
    assert scenario["order"]["quoteTotal"] == pytest.approx(
        sum(l["lineTotal"] for l in lines)
    )


def test_line_totals_are_price_times_quantity(scenario):
    for l in scenario["order"]["lines"]:
        assert l["lineTotal"] == pytest.approx(l["sellingPrice"] * l["quantity"])


def test_order_produces_a_mix_of_short_and_covered_lines(scenario):
    """The demo needs one cheap shortage, one expensive one, and one covered."""
    short = {s["skuId"]: s["shortageQty"] for s in scenario["shortages"]}
    assert short["SW-ANC-1W10A"] == 6
    assert short["W-FIN-1.5-RED-90M"] == 2
    assert short["MCB-HAV-SP-32A-C"] == 0


def test_supplier_price_rise_on_the_wire_is_surfaced(scenario):
    rise = {d["skuId"]: d for d in scenario["priceChanges"]}["W-FIN-1.5-RED-90M"]
    assert rise["previousCost"] == 5900.0
    assert rise["currentCost"] == 6300.0
    assert rise["percentChange"] == pytest.approx(6.78, abs=0.01)
    assert rise["isAlert"] is True


def test_trivial_price_drift_is_reported_but_not_alerted(scenario):
    minor = {d["skuId"]: d for d in scenario["priceChanges"]}["SW-ANC-1W10A"]
    assert minor["percentChange"] < 5.0
    assert minor["isAlert"] is False


def test_budget_genuinely_binds(scenario):
    assert_budget_binds(scenario)


def test_every_commitment_is_funded_at_the_demo_budget(scenario):
    plan = scenario["budgetPlan"]
    assert plan["allCommitmentsFunded"] is True
    committed = [l for l in plan["lines"] if l["tier"] == "COMMITTED_ORDER"]
    assert committed and all(l["decision"] == "BUY" for l in committed)


def test_some_restocking_is_refused_for_lack_of_cash(scenario):
    refused = [d for d in scenario["budgetPlan"]["deferred"]
               if d["tier"] == "RESTOCK" and d["fundedQty"] == 0]
    assert len(refused) > 0


def test_plan_spends_within_budget_and_accounts_for_every_rupee(scenario):
    plan = scenario["budgetPlan"]
    assert plan["budget"] == DEMO_BUDGET
    assert plan["totalSpend"] <= DEMO_BUDGET + 1e-6
    assert plan["totalSpend"] == pytest.approx(plan["tier1Spend"] + plan["tier2Spend"])
    assert plan["remaining"] == pytest.approx(DEMO_BUDGET - plan["totalSpend"])
    assert plan["remaining"] >= 0


def test_the_budget_is_nearly_exhausted(scenario):
    """A budget that leaves most of itself unspent is not a real constraint."""
    plan = scenario["budgetPlan"]
    assert plan["totalSpend"] > 0.95 * DEMO_BUDGET


def test_every_decision_carries_a_reason(scenario):
    for line in scenario["budgetPlan"]["lines"]:
        assert line["reason"]
        if line["decision"] in ("DEFER", "PARTIAL"):
            assert line["risk"], f"{line['skuId']} deferred without a stated risk"


def test_every_line_cost_is_unit_cost_times_funded_quantity(scenario):
    for line in scenario["budgetPlan"]["lines"]:
        assert line["lineCost"] == pytest.approx(
            line["unitCost"] * line["fundedQty"], abs=0.01
        )


def test_cheaper_supplier_option_is_advisory_only(scenario):
    """A rival quote is surfaced, but never silently spent against."""
    alts = {a["skuId"] for a in scenario["cheaperAlternatives"]}
    assert "MCB-HAV-SP-32A-C" in alts

    plan_costs = {l["skuId"]: l["unitCost"] for l in scenario["budgetPlan"]["lines"]}
    for a in scenario["cheaperAlternatives"]:
        if a["skuId"] in plan_costs:
            assert plan_costs[a["skuId"]] == a["incumbentCost"]


def test_scenario_is_reproducible():
    assert build_scenario() == build_scenario()
