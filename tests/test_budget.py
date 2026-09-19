"""Two-tier budget allocation.

The policy under test:
  Tier 1  committed customer orders, earliest promised date first
  Tier 2  discretionary restocking, priority = stockoutRisk * marginPerRupee
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_order, make_product
from engine.budget import (
    BUY,
    DEFER,
    PARTIAL,
    REORDER_COVER_WEEKS,
    SAFETY_WEEKS,
    TIER1,
    TIER2,
    allocate_budget,
    restock_candidates,
)


def lines_by_sku(plan, tier):
    return {l.skuId: l for l in plan.lines if l.tier == tier}


# ---------------------------------------------------------------------------
# Test A - budget is sufficient
# ---------------------------------------------------------------------------

def test_A_sufficient_budget_funds_every_committed_shortage():
    products = [make_product("P", 1000, 1500), make_product("Q", 500, 700)]
    data = make_dataset(
        products,
        inventory={"P": 1, "Q": 0},
        velocity={"P": 1, "Q": 1},
        orders=[make_order("O1", {"P": 3, "Q": 4})],
    )
    plan = allocate_budget(data, budget=100_000)

    tier1 = lines_by_sku(plan, TIER1)
    assert tier1["P"].decision == BUY and tier1["P"].fundedQty == 2
    assert tier1["Q"].decision == BUY and tier1["Q"].fundedQty == 4
    assert plan.allCommitmentsFunded is True
    assert plan.tier1Spend == pytest.approx(2 * 1000 + 4 * 500)


# ---------------------------------------------------------------------------
# Test B - budget is insufficient
# ---------------------------------------------------------------------------

def test_B_committed_orders_are_funded_before_any_restocking():
    """A restock with far better economics must still lose to a promise."""
    products = [
        make_product("P", 1000, 1100),          # commitment, thin margin
        make_product("R", 10, 100),             # restock, excellent margin
    ]
    data = make_dataset(
        products,
        inventory={"P": 0, "R": 0},
        velocity={"P": 1, "R": 10},
        orders=[make_order("O1", {"P": 5})],
    )
    plan = allocate_budget(data, budget=5000)

    assert lines_by_sku(plan, TIER1)["P"].decision == BUY
    assert plan.tier1Spend == 5000
    assert plan.tier2Spend == 0
    assert lines_by_sku(plan, TIER2)["R"].decision == DEFER


def test_B_insufficient_budget_prefers_the_earliest_promised_order():
    products = [make_product("P", 1000, 1500), make_product("Q", 1000, 1500)]
    data = make_dataset(
        products,
        inventory={"P": 0, "Q": 0},
        velocity={"P": 1, "Q": 1},
        orders=[
            make_order("LATE", {"Q": 5}, promisedDate="2026-09-25"),
            make_order("EARLY", {"P": 5}, promisedDate="2026-09-20"),
        ],
    )
    plan = allocate_budget(data, budget=5000)

    tier1 = lines_by_sku(plan, TIER1)
    assert tier1["P"].decision == BUY and tier1["P"].fundedQty == 5
    assert tier1["Q"].decision == DEFER and tier1["Q"].fundedQty == 0
    assert plan.allCommitmentsFunded is False


def test_B_partial_fill_reports_the_remaining_shortfall():
    products = [make_product("P", 1000, 1500)]
    data = make_dataset(products, inventory={"P": 0}, velocity={"P": 1},
                        orders=[make_order("O1", {"P": 5})])
    plan = allocate_budget(data, budget=2500)

    line = lines_by_sku(plan, TIER1)["P"]
    assert line.decision == PARTIAL
    assert line.fundedQty == 2          # 2500 buys two whole units
    assert line.lineCost == 2000
    assert "3 units still short" in line.risk


# ---------------------------------------------------------------------------
# Test C - budget remains after commitments
# ---------------------------------------------------------------------------

def test_C_leftover_budget_follows_restock_priority_not_stockout_risk_alone():
    """Y is better stocked than X but earns far more per rupee, so Y wins.

    X: coverage 0.0 wk -> risk 1.0, margin/rupee 0.5 -> priority 0.50
    Y: coverage 1.5 wk -> risk 0.5, margin/rupee 2.0 -> priority 1.00
    """
    products = [make_product("X", 100, 150), make_product("Y", 100, 300)]
    data = make_dataset(products, inventory={"X": 0, "Y": 15},
                        velocity={"X": 10, "Y": 10})

    ranked = restock_candidates(data)
    assert [c.skuId for c in ranked] == ["Y", "X"]
    assert ranked[0].priority == pytest.approx(1.0)
    assert ranked[1].priority == pytest.approx(0.5)

    # Reorder targets lead time + safety + cover weeks of demand.
    target = 10 * (1.0 + SAFETY_WEEKS + REORDER_COVER_WEEKS)
    assert ranked[0].reorderQty == target - 15
    assert ranked[1].reorderQty == target

    plan = allocate_budget(data, budget=6000)
    tier2 = lines_by_sku(plan, TIER2)
    assert tier2["Y"].decision == BUY and tier2["Y"].lineCost == 5500
    assert tier2["X"].decision == PARTIAL and tier2["X"].fundedQty == 5
    assert plan.remaining == 0


def test_C_restocking_only_uses_what_tier_1_left_behind():
    products = [make_product("P", 1000, 1500), make_product("X", 100, 300)]
    data = make_dataset(products, inventory={"P": 0, "X": 0},
                        velocity={"P": 1, "X": 10},
                        orders=[make_order("O1", {"P": 2})])
    plan = allocate_budget(data, budget=10_000)

    assert plan.tier1Spend == 2000
    assert plan.tier2Spend == pytest.approx(plan.totalSpend - 2000)
    assert plan.totalSpend <= 10_000


# ---------------------------------------------------------------------------
# Test D - deferral carries a calculated reason and risk
# ---------------------------------------------------------------------------

def test_D_deferred_restock_explains_the_risk_with_real_numbers():
    products = [make_product("X", 100, 150)]
    data = make_dataset(products, inventory={"X": 5}, velocity={"X": 10})
    plan = allocate_budget(data, budget=0)

    line = lines_by_sku(plan, TIER2)["X"]
    assert line.decision == DEFER
    assert line.fundedQty == 0
    # 5 units at 10/week is half a week of cover against a 7-day lead time.
    assert "0.5 weeks" in line.risk
    assert "10.0 units/week" in line.risk
    assert "7 days" in line.risk
    assert "run out before a replacement order could arrive" in line.risk


def test_D_deferral_evidence_exposes_the_full_priority_calculation():
    products = [make_product("X", 100, 150)]
    data = make_dataset(products, inventory={"X": 5}, velocity={"X": 10})
    plan = allocate_budget(data, budget=0)

    ev = lines_by_sku(plan, TIER2)["X"].evidence
    assert ev["formula"] == "priority = stockoutRisk * marginPerRupee"
    assert ev["weeklyVelocity"] == 10.0
    assert ev["coverageWeeks"] == 0.5
    assert ev["leadTimeDays"] == 7
    assert ev["marginPerRupee"] == pytest.approx(0.5)
    # Evidence values are rounded for display, so compare within that tolerance.
    assert ev["priority"] == pytest.approx(
        ev["stockoutRisk"] * ev["marginPerRupee"], abs=1e-4
    )


def test_D_unfunded_commitment_explains_the_delivery_risk():
    products = [make_product("P", 1000, 1500)]
    data = make_dataset(products, inventory={"P": 0}, velocity={"P": 1},
                        orders=[make_order("O1", {"P": 4})])
    plan = allocate_budget(data, budget=0)

    line = lines_by_sku(plan, TIER1)["P"]
    assert line.decision == DEFER
    assert "cannot be delivered" in line.risk


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_dead_stock_is_never_restocked():
    products = [make_product("DEAD", 100, 500)]
    data = make_dataset(products, inventory={"DEAD": 80}, velocity={"DEAD": 0})
    assert restock_candidates(data) == []
    assert allocate_budget(data, 100_000).totalSpend == 0


def test_comfortably_stocked_item_is_not_restocked():
    # 100 units at 10/week is 10 weeks cover against a 3-week risk horizon.
    products = [make_product("X", 100, 150)]
    data = make_dataset(products, inventory={"X": 100}, velocity={"X": 10})
    assert restock_candidates(data) == []


def test_zero_budget_funds_nothing_but_still_reports_every_decision():
    products = [make_product("P", 1000, 1500), make_product("X", 100, 150)]
    data = make_dataset(products, inventory={"P": 0, "X": 0},
                        velocity={"P": 1, "X": 10},
                        orders=[make_order("O1", {"P": 2})])
    plan = allocate_budget(data, budget=0)

    assert plan.totalSpend == 0
    assert plan.remaining == 0
    assert plan.purchased == []
    assert {l.decision for l in plan.lines} == {DEFER}
    # P appears twice by design: once to honour the order, and again as a
    # restock, since filling the order leaves nothing for walk-in demand.
    assert [(l.skuId, l.tier) for l in plan.lines] == [
        ("P", TIER1), ("P", TIER2), ("X", TIER2),
    ]


def test_negative_budget_is_rejected():
    data = make_dataset([make_product("P", 100, 150)])
    with pytest.raises(ValueError):
        allocate_budget(data, budget=-1)


def test_plan_never_overspends_the_budget():
    products = [make_product(f"S{i}", 100 + i, 300) for i in range(20)]
    data = make_dataset(
        products,
        inventory={f"S{i}": 0 for i in range(20)},
        velocity={f"S{i}": 5 for i in range(20)},
        orders=[make_order("O1", {"S0": 10})],
    )
    for budget in (0, 137.5, 1000, 9999.99, 50_000):
        plan = allocate_budget(data, budget)
        assert plan.totalSpend <= budget + 1e-6
        assert plan.remaining >= -1e-6
        assert plan.totalSpend == pytest.approx(plan.tier1Spend + plan.tier2Spend)


def test_allocation_is_deterministic():
    def build():
        products = [make_product(f"S{i}", 100 + i, 300 - i) for i in range(12)]
        return make_dataset(products,
                            inventory={f"S{i}": i for i in range(12)},
                            velocity={f"S{i}": 4 for i in range(12)})
    first = allocate_budget(build(), 7500).as_dict()
    second = allocate_budget(build(), 7500).as_dict()
    assert first == second
