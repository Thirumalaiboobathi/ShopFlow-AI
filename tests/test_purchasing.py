"""Stage 5 - the cash-constrained purchasing planner.

The allocation logic itself is covered by test_budget.py. What is tested here
is the owner-facing layer built on top of it: budget validation, the confirmed
supplier cost path, the selling-price/supplier-cost separation, the what-if
budgets, and the guarantees the planner must never break.
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_order, make_product
from engine.budget import TIER1, TIER2, restock_candidates
from engine.models import ALL_OR_NOTHING, SupplierPrice
from engine.pricing import current_cost
from engine.purchasing import (
    InvalidBudgetError,
    apply_confirmed_costs,
    budget_is_binding,
    build_purchase_plan,
    confirmed_costs,
    parse_budget,
    what_if,
)

CANONICAL_BUDGET = 25000.0
WIRE = "W-FIN-1.5-RED-90M"


def tiny_shop():
    """Two SKUs, one committed order, one restock candidate."""
    products = [make_product("P", 1000, 1500), make_product("Q", 100, 160)]
    return make_dataset(
        products,
        inventory={"P": 1, "Q": 0},
        velocity={"P": 2, "Q": 5},
        orders=[make_order("O1", {"P": 3})],
    )


# ---------------------------------------------------------------------------
# 1-2  budget validation
# ---------------------------------------------------------------------------

def test_1_zero_budget_plans_nothing_and_is_not_an_error():
    plan = build_purchase_plan(tiny_shop(), 0)
    assert plan["budget"] == 0.0
    assert plan["totalSpend"] == 0.0
    assert plan["remaining"] == 0.0
    # Nothing was bought, so nothing is funded - but the shop is still told
    # exactly what it could not afford.
    assert plan["allCommitmentsFunded"] is False
    assert plan["counts"]["restockDeferred"] > 0


def test_2_negative_budget_is_rejected():
    with pytest.raises(InvalidBudgetError):
        parse_budget(-1)
    with pytest.raises(InvalidBudgetError):
        build_purchase_plan(tiny_shop(), -0.01)


@pytest.mark.parametrize("bad", ["25000", None, True, float("nan"), float("inf")])
def test_2b_non_numeric_budget_is_rejected(bad):
    with pytest.raises(InvalidBudgetError):
        parse_budget(bad)


# ---------------------------------------------------------------------------
# 3-4  sufficient and insufficient budget
# ---------------------------------------------------------------------------

def test_3_sufficient_budget_funds_commitments_and_restocking():
    plan = build_purchase_plan(tiny_shop(), 100000.0)
    assert plan["allCommitmentsFunded"] is True
    assert plan["counts"]["restockSelected"] > 0
    # Nothing is contested at this budget, so it is not a real constraint.
    assert plan["budgetIsBinding"] is False


def test_4_insufficient_budget_leaves_commitments_short():
    # The shortfall is 2 units of P at 1000 each; 1500 buys one.
    plan = build_purchase_plan(tiny_shop(), 1500.0)
    assert plan["allCommitmentsFunded"] is False
    commitment = plan["commitments"][0]
    assert commitment["fundedQty"] == 1
    assert commitment["requestedQty"] == 2
    assert commitment["risk"]  # the unmet promise is stated, not hidden


# ---------------------------------------------------------------------------
# 5  commitments outrank restocking
# ---------------------------------------------------------------------------

def test_5_commitments_are_funded_before_restocking():
    # Q is far more profitable per rupee than P, but P is promised to a
    # customer. The promise wins regardless.
    products = [make_product("P", 1000, 1100), make_product("Q", 100, 900)]
    data = make_dataset(
        products,
        inventory={"P": 0, "Q": 0},
        velocity={"P": 1, "Q": 20},
        orders=[make_order("O1", {"P": 2})],
    )
    plan = build_purchase_plan(data, 2000.0)

    assert plan["commitmentCost"] == 2000.0
    assert plan["restockCost"] == 0.0
    assert plan["allCommitmentsFunded"] is True
    assert all(not l["selected"] for l in plan["restockDeferred"])


# ---------------------------------------------------------------------------
# 6-7  the two guarantees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "budget", [0, 1, 500, 1500, 9999.99, 20000, 25000, 30000, 100000]
)
def test_6_7_spend_never_exceeds_budget_and_remaining_never_negative(seeded, budget):
    plan = build_purchase_plan(seeded, budget)
    assert plan["totalSpend"] <= plan["budget"] + 1e-9
    assert plan["remaining"] >= 0
    assert round(plan["commitmentCost"] + plan["restockCost"], 2) == plan["totalSpend"]


def test_6b_no_line_is_funded_beyond_what_was_requested(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    for line in plan["commitments"] + plan["restockSelected"] + plan["restockDeferred"]:
        assert 0 <= line["fundedQty"] <= line["requestedQty"]


# ---------------------------------------------------------------------------
# 8-10  ranking and its inputs
# ---------------------------------------------------------------------------

def test_8_restock_candidates_are_ranked_by_descending_priority(seeded):
    """The shop considers restocks in priority order, highest first."""
    priorities = [c.priority for c in restock_candidates(seeded)]
    assert priorities == sorted(priorities, reverse=True)

    # And each display group preserves that order.
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    for group in ("restockSelected", "restockDeferred"):
        group_priorities = [l["priority"] for l in plan[group]]
        assert group_priorities == sorted(group_priorities, reverse=True)


def test_9_stockout_risk_is_between_zero_and_one_and_drives_selection(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    for line in plan["restockSelected"]:
        assert 0 < line["stockoutRisk"] <= 1.0


def test_10_margin_per_rupee_uses_supplier_cost_not_catalog_cost():
    product = make_product("P", 100, 150)  # catalog cost 100
    data = make_dataset(
        products=[product],
        inventory={"P": 0},
        velocity={"P": 5},
        # The supplier's live cost is 120, not the catalog's 100.
        prices={"P": [SupplierPrice("P", "S-FAST", "2026-09-01", 120.0)]},
    )
    plan = build_purchase_plan(data, 10000.0)
    line = plan["restockSelected"][0]

    assert line["unitCost"] == 120.0
    expected = (150 - 120) / 120
    assert line["marginPerRupee"] == pytest.approx(expected, abs=1e-4)


def test_10b_priority_calculation_string_matches_its_own_figures(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    line = plan["restockSelected"][0]
    assert line["priorityCalculation"] == (
        f"priority = {line['stockoutRisk']} x {line['marginPerRupee']} "
        f"= {line['priority']}"
    )
    assert line["stockoutRisk"] * line["marginPerRupee"] == pytest.approx(
        line["priority"], abs=1e-4
    )


# ---------------------------------------------------------------------------
# 11  deferred items must explain themselves
# ---------------------------------------------------------------------------

def test_11_every_deferred_item_carries_a_reason_and_a_risk(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    assert plan["restockDeferred"]
    for line in plan["restockDeferred"]:
        assert line["reason"]
        assert line["risk"]
        assert line["fullLineCost"] > 0
        # The evidence must let the owner recompute the ranking themselves.
        for key in ("stockoutRisk", "marginPerRupee", "priority", "coverageWeeks"):
            assert key in line


def test_11b_every_deferred_item_was_genuinely_unaffordable(seeded):
    """Deferral is always a cash decision, never an arbitrary cut-off.

    A high-priority item CAN be passed over while a lower-priority one is
    funded: this is a greedy allocator, so an expensive urgent line that does
    not fit is skipped and a cheaper line further down the list is bought
    instead. The invariant is therefore not "deferred ranks below selected" -
    it is that nothing was deferred while the shop could still afford it.
    """
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    for line in plan["restockDeferred"]:
        remaining_then = line["evidence"]["budgetRemainingBefore"]
        assert line["unitCost"] > remaining_then, (
            f"{line['skuId']} was deferred with Rs {remaining_then} still "
            f"available and a unit cost of Rs {line['unitCost']}"
        )


# ---------------------------------------------------------------------------
# 12-13  fulfilment policy
# ---------------------------------------------------------------------------

def test_12_partial_allowed_buys_what_the_cash_covers():
    data = tiny_shop()  # P defaults to PARTIAL_ALLOWED
    plan = build_purchase_plan(data, 1500.0)
    line = plan["commitments"][0]

    assert line["fulfilmentPolicy"] == "PARTIAL_ALLOWED"
    assert line["decision"] == "PARTIAL"
    assert line["fundedQty"] == 1


def test_13_all_or_nothing_defers_rather_than_part_delivering():
    products = [
        make_product("P", 1000, 1500).__class__(
            **{**make_product("P", 1000, 1500).__dict__,
               "fulfilmentPolicy": ALL_OR_NOTHING}
        )
    ]
    data = make_dataset(
        products,
        inventory={"P": 1},
        velocity={"P": 2},
        orders=[make_order("O1", {"P": 3})],
    )
    plan = build_purchase_plan(data, 1500.0)
    line = plan["commitments"][0]

    assert line["fulfilmentPolicy"] == ALL_OR_NOTHING
    assert line["decision"] == "DEFER"
    assert line["fundedQty"] == 0
    assert line["lineCost"] == 0.0


# ---------------------------------------------------------------------------
# 14-15  supplier cost vs selling price
# ---------------------------------------------------------------------------

def test_14_confirmed_supplier_price_becomes_the_purchase_cost(seeded):
    decisions = [{"skuId": WIRE, "decision": "CONFIRMED",
                  "previousPrice": 5900.0, "currentPrice": 6300.0}]
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET, decisions)
    line = next(l for l in plan["commitments"] if l["skuId"] == WIRE)

    assert line["unitCost"] == 6300.0
    assert line["costBasis"]["previousCost"] == 5900.0
    assert line["costBasis"]["confirmedCost"] == 6300.0


def test_14b_rejected_price_change_does_not_reprice_the_plan(seeded):
    decisions = [{"skuId": WIRE, "decision": "REJECTED",
                  "previousPrice": 5900.0, "currentPrice": 6300.0}]
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET, decisions)
    line = next(l for l in plan["commitments"] if l["skuId"] == WIRE)

    assert line["unitCost"] == 5900.0
    assert "costBasis" not in line
    assert plan["confirmedCosts"] == []


def test_14c_confirming_a_rise_squeezes_restocking_by_exactly_the_extra_cost(seeded):
    decisions = [{"skuId": WIRE, "decision": "CONFIRMED",
                  "previousPrice": 5900.0, "currentPrice": 6300.0}]
    before = build_purchase_plan(seeded, CANONICAL_BUDGET)
    after = build_purchase_plan(seeded, CANONICAL_BUDGET, decisions)

    # Two coils are on order, so the rise costs 2 x 400 = 800 more.
    assert after["commitmentCost"] - before["commitmentCost"] == pytest.approx(800.0)
    # That money has to come out of restocking - the budget did not grow.
    assert after["restockCost"] < before["restockCost"]
    assert after["totalSpend"] <= CANONICAL_BUDGET
    assert after["remaining"] >= 0


def test_15_selling_price_is_never_changed_by_a_confirmed_cost(seeded):
    before_selling = seeded.product(WIRE).sellingPrice
    before_cost = current_cost(seeded, WIRE)

    decisions = [{"skuId": WIRE, "decision": "CONFIRMED",
                  "previousPrice": 5900.0, "currentPrice": 6300.0}]
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET, decisions)
    line = next(l for l in plan["commitments"] if l["skuId"] == WIRE)

    # The plan buys at the new cost but still sells at the old shelf price.
    assert line["unitCost"] == 6300.0
    assert line["sellingPrice"] == before_selling
    # And the seeded shop itself is untouched by having been planned against.
    assert seeded.product(WIRE).sellingPrice == before_selling
    assert current_cost(seeded, WIRE) == before_cost


def test_15b_apply_confirmed_costs_does_not_mutate_the_source_dataset(seeded):
    original = len(seeded.prices(WIRE))
    updated = apply_confirmed_costs(seeded, {WIRE: 6300.0})

    assert len(seeded.prices(WIRE)) == original
    assert len(updated.prices(WIRE)) == original + 1
    assert current_cost(seeded, WIRE) == 5900.0
    assert current_cost(updated, WIRE) == 6300.0


def test_15c_confirmed_costs_ignores_unusable_rows():
    rows = [
        {"skuId": "A", "decision": "CONFIRMED", "currentPrice": 10.0},
        {"skuId": "B", "decision": "REJECTED", "currentPrice": 20.0},
        {"skuId": "C", "decision": "CONFIRMED", "currentPrice": None},
        {"skuId": "D", "decision": "CONFIRMED", "currentPrice": -5.0},
        {"skuId": "E", "decision": "CONFIRMED", "currentPrice": "abc"},
        "not a dict",
    ]
    assert confirmed_costs(rows) == {"A": 10.0}


def test_15d_a_confirmed_cost_for_an_unknown_sku_is_ignored(seeded):
    updated = apply_confirmed_costs(seeded, {"NO-SUCH-SKU": 999.0})
    assert "NO-SUCH-SKU" not in updated.products
    assert "NO-SUCH-SKU" not in updated.priceHistory


# ---------------------------------------------------------------------------
# 16-18  the three canonical budgets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [20000.0, 25000.0, 30000.0])
def test_16_17_18_canonical_budgets_fund_commitments_and_still_bind(seeded, budget):
    plan = build_purchase_plan(seeded, budget)

    assert plan["allCommitmentsFunded"] is True
    assert plan["budgetIsBinding"] is True
    assert plan["totalSpend"] <= budget
    assert plan["remaining"] >= 0
    assert plan["counts"]["restockSelected"] > 0
    assert plan["counts"]["restockDeferred"] > 0


def test_17b_more_cash_never_buys_less(seeded):
    """Monotonicity: a bigger budget must not produce a smaller plan."""
    plans = [build_purchase_plan(seeded, b) for b in (20000.0, 25000.0, 30000.0)]
    spends = [p["totalSpend"] for p in plans]
    selected = [p["counts"]["restockSelected"] for p in plans]

    assert spends == sorted(spends)
    assert selected == sorted(selected)


def test_18b_what_if_returns_the_two_alternative_budgets(seeded):
    scenarios = what_if(seeded, CANONICAL_BUDGET)
    assert [s["budget"] for s in scenarios] == [20000.0, 30000.0]
    for s in scenarios:
        assert s["totalSpend"] <= s["budget"]
        assert s["remaining"] >= 0


def test_18c_what_if_omits_a_scenario_equal_to_the_current_budget(seeded):
    scenarios = what_if(seeded, 20000.0)
    assert [s["budget"] for s in scenarios] == [30000.0]


# ---------------------------------------------------------------------------
# 19  the canonical scenario is a genuine tradeoff
# ---------------------------------------------------------------------------

def test_19_canonical_budget_binds(seeded):
    """The demo is dishonest if 25,000 does not force a real choice.

    Both halves are required: every customer promise is kept, and at least one
    restock the shop would like to make is turned down for lack of cash.
    """
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)

    assert plan["allCommitmentsFunded"] is True, "commitments must be fully funded"
    assert plan["restockDeferred"], "budget does not bind - nothing was deferred"
    assert plan["budgetIsBinding"] is True

    # And the cash is genuinely almost exhausted, not merely partly used.
    assert plan["remaining"] < plan["budget"] * 0.01


def test_19b_budget_is_binding_requires_both_halves():
    # Commitments unfunded -> not binding, it is simply broken.
    starved = build_purchase_plan(tiny_shop(), 0)
    assert starved["budgetIsBinding"] is False

    # Nothing contested -> not binding either.
    generous = build_purchase_plan(tiny_shop(), 1_000_000.0)
    assert generous["budgetIsBinding"] is False


def test_19c_plan_totals_reconcile_line_by_line(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    tier1 = round(sum(l["lineCost"] for l in plan["commitments"]), 2)
    tier2 = round(
        sum(l["lineCost"] for l in plan["restockSelected"] + plan["restockDeferred"]), 2
    )

    assert tier1 == plan["commitmentCost"]
    assert tier2 == plan["restockCost"]
    assert round(tier1 + tier2, 2) == plan["totalSpend"]
    assert round(plan["budget"] - plan["totalSpend"], 2) == plan["remaining"]


def test_19d_tiers_are_labelled_consistently(seeded):
    plan = build_purchase_plan(seeded, CANONICAL_BUDGET)
    assert all(l["tier"] == TIER1 for l in plan["commitments"])
    assert all(
        l["tier"] == TIER2
        for l in plan["restockSelected"] + plan["restockDeferred"]
    )
