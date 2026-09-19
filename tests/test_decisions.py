"""Behaviour agreed at the Stage 1 review.

1. Tier 1 and Tier 2 stay separate in the engine; the UI merges by SKU.
2. Supplier price alerts default to 5%, configurable.
3. Fulfilment policy is explicit: PARTIAL_ALLOWED vs ALL_OR_NOTHING.
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_order, make_product
from engine.budget import BUY, DEFER, TIER1, TIER2, allocate_budget
from engine.models import ALL_OR_NOTHING, PARTIAL_ALLOWED, Product
from engine.pricing import PRICE_ALERT_THRESHOLD_PERCENT, detect_price_increases
from engine.models import SupplierPrice


# ---- Decision 1: merged display, separate internals ----

def test_engine_keeps_the_two_tier_allocations_separate():
    products = [make_product("P", 100, 300)]
    data = make_dataset(products, inventory={"P": 0}, velocity={"P": 10},
                        orders=[make_order("O1", {"P": 6})])
    plan = allocate_budget(data, budget=100_000)

    tiers = [(l.tier, l.fundedQty) for l in plan.lines if l.skuId == "P"]
    assert tiers == [(TIER1, 6), (TIER2, 70)]


def test_merged_view_combines_the_same_sku_but_retains_the_split():
    products = [make_product("P", 100, 300)]
    data = make_dataset(products, inventory={"P": 0}, velocity={"P": 10},
                        orders=[make_order("O1", {"P": 6})])
    plan = allocate_budget(data, budget=100_000)

    merged = {m["skuId"]: m for m in plan.merged_lines()}["P"]
    assert merged["totalFundedQty"] == 76
    assert merged["totalCost"] == pytest.approx(7600)
    assert [t["tier"] for t in merged["tiers"]] == [TIER1, TIER2]
    assert [t["fundedQty"] for t in merged["tiers"]] == [6, 70]


def test_merged_totals_reconcile_with_the_plan_total():
    products = [make_product(f"S{i}", 100 + i, 300) for i in range(6)]
    data = make_dataset(products,
                        inventory={f"S{i}": i for i in range(6)},
                        velocity={f"S{i}": 5 for i in range(6)},
                        orders=[make_order("O1", {"S0": 4, "S1": 3})])
    plan = allocate_budget(data, budget=9000)
    assert sum(m["totalCost"] for m in plan.merged_lines()) == pytest.approx(
        plan.totalSpend
    )


# ---- Decision 2: 5% default alert threshold ----

def test_default_price_alert_threshold_is_five_percent():
    assert PRICE_ALERT_THRESHOLD_PERCENT == 5.0


def test_small_price_drift_is_not_alerted_by_default():
    data = make_dataset([make_product("A", 100, 150)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0),
              SupplierPrice("A", "S-FAST", "2026-09-01", 103.0)],   # +3%
    })
    assert detect_price_increases(data) == []
    # Still reachable for analysis with an explicit threshold.
    assert len(detect_price_increases(data, threshold_percent=1.0)) == 1


def test_material_price_rise_is_alerted_by_default():
    data = make_dataset([make_product("A", 100, 150)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 5900.0),
              SupplierPrice("A", "S-FAST", "2026-09-01", 6300.0)],  # +6.78%
    })
    assert [d.skuId for d in detect_price_increases(data)] == ["A"]


def test_seeded_demo_uses_the_five_percent_threshold(seeded):
    """At 5% the planted wire rise stands out instead of drowning in noise."""
    alerts = detect_price_increases(seeded)
    assert "W-FIN-1.5-RED-90M" in {d.skuId for d in alerts}
    assert len(alerts) < len(detect_price_increases(seeded, threshold_percent=1.0))


# ---- Decision 3: explicit fulfilment policy ----

def make_indivisible(skuId: str, cost: float, selling: float) -> Product:
    return Product(
        skuId=skuId, brand="B", category="Ceiling Fan", specification="1200mm",
        colour=None, length=None, unit="piece", sellingPrice=selling,
        costPrice=cost, supplierId="S-FAST", name=skuId,
        fulfilmentPolicy=ALL_OR_NOTHING,
    )


def test_products_allow_partial_fulfilment_by_default():
    assert make_product("P", 100, 150).fulfilmentPolicy == PARTIAL_ALLOWED


def test_all_or_nothing_line_is_deferred_rather_than_part_funded():
    data = make_dataset([make_indivisible("KIT", 1000, 1500)],
                        inventory={"KIT": 0}, velocity={"KIT": 1},
                        orders=[make_order("O1", {"KIT": 5})])
    plan = allocate_budget(data, budget=2500)   # affords 2 of 5

    line = [l for l in plan.lines if l.tier == TIER1][0]
    assert line.decision == DEFER
    assert line.fundedQty == 0
    assert line.lineCost == 0
    assert "cannot be part-delivered" in line.reason
    assert line.evidence["fulfilmentPolicy"] == ALL_OR_NOTHING


def test_all_or_nothing_line_is_funded_when_budget_covers_it_fully():
    data = make_dataset([make_indivisible("KIT", 1000, 1500)],
                        inventory={"KIT": 0}, velocity={"KIT": 1},
                        orders=[make_order("O1", {"KIT": 5})])
    plan = allocate_budget(data, budget=5000)

    line = [l for l in plan.lines if l.tier == TIER1][0]
    assert line.decision == BUY and line.fundedQty == 5


def test_partial_allowed_line_is_still_part_funded():
    data = make_dataset([make_product("P", 1000, 1500)],
                        inventory={"P": 0}, velocity={"P": 1},
                        orders=[make_order("O1", {"P": 5})])
    plan = allocate_budget(data, budget=2500)
    assert [l for l in plan.lines if l.tier == TIER1][0].fundedQty == 2


def test_deferring_an_indivisible_line_leaves_cash_for_the_next_commitment():
    """The skipped budget must not be stranded - it funds the next promise."""
    products = [make_indivisible("KIT", 1000, 1500), make_product("P", 100, 150)]
    data = make_dataset(products, inventory={"KIT": 0, "P": 0},
                        velocity={"KIT": 1, "P": 1},
                        orders=[
                            make_order("A", {"KIT": 5}, promisedDate="2026-09-20"),
                            make_order("B", {"P": 10}, promisedDate="2026-09-22"),
                        ])
    plan = allocate_budget(data, budget=2500)

    lines = {l.skuId: l for l in plan.lines if l.tier == TIER1}
    assert lines["KIT"].decision == DEFER
    assert lines["P"].decision == BUY and lines["P"].fundedQty == 10


def test_seeded_demo_wire_shortage_is_a_meaningful_partial_case(seeded):
    """The demo's expensive shortage is coils - part-delivery genuinely helps."""
    wire = seeded.product("W-FIN-1.5-RED-90M")
    assert wire.fulfilmentPolicy == PARTIAL_ALLOWED
    assert wire.unit == "coil"
