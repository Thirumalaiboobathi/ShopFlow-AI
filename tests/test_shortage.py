"""Committed demand and shortage calculation."""

from __future__ import annotations

from conftest import make_dataset, make_order, make_product
from engine.shortage import (
    committed_demand,
    earliest_promise,
    shortage_qty,
    shortages,
    uncommitted_stock,
)


def test_shortage_is_demand_minus_stock():
    assert shortage_qty(committed=20, on_hand=14) == 6


def test_no_shortage_when_stock_covers_demand():
    assert shortage_qty(committed=2, on_hand=5) == 0


def test_shortage_never_negative():
    # Surplus stock is not negative demand.
    assert shortage_qty(committed=1, on_hand=99) == 0


def test_negative_inventory_treated_as_empty():
    # A stock-take error must not create phantom availability.
    assert shortage_qty(committed=5, on_hand=-3) == 5


def test_demand_sums_across_multiple_orders():
    orders = [make_order("O1", {"A": 5}), make_order("O2", {"A": 7, "B": 2})]
    assert committed_demand(orders) == {"A": 12, "B": 2}


def test_uncommitted_orders_are_excluded():
    orders = [
        make_order("O1", {"A": 5}),
        make_order("O2", {"A": 100}, committed=False),
    ]
    assert committed_demand(orders) == {"A": 5}


def test_earliest_promise_per_sku():
    orders = [
        make_order("O1", {"A": 1}, promisedDate="2026-09-25"),
        make_order("O2", {"A": 1, "B": 1}, promisedDate="2026-09-20"),
    ]
    assert earliest_promise(orders) == {"A": "2026-09-20", "B": "2026-09-20"}


def test_shortages_include_covered_lines_with_zero_shortage():
    # The UI needs to show "no action needed", not silently drop the line.
    products = [make_product("A", 100, 150), make_product("B", 100, 150)]
    data = make_dataset(products, inventory={"A": 1, "B": 50},
                        orders=[make_order("O1", {"A": 3, "B": 2})])
    result = {s.skuId: s for s in shortages(data)}
    assert result["A"].shortageQty == 2
    assert result["B"].shortageQty == 0
    assert result["B"].isShort is False


def test_uncommitted_stock_reserves_promised_units():
    # 10 on hand with 8 promised leaves 2 for walk-in demand, not 10.
    products = [make_product("A", 100, 150)]
    data = make_dataset(products, inventory={"A": 10},
                        orders=[make_order("O1", {"A": 8})])
    assert uncommitted_stock(data, "A", committed_demand(data.committedOrders())) == 2


def test_uncommitted_stock_floors_at_zero_when_oversold():
    products = [make_product("A", 100, 150)]
    data = make_dataset(products, inventory={"A": 2},
                        orders=[make_order("O1", {"A": 8})])
    assert uncommitted_stock(data, "A", committed_demand(data.committedOrders())) == 0


def test_seeded_demo_order_shortages(seeded):
    result = {s.skuId: s for s in shortages(seeded)}
    assert result["SW-ANC-1W10A"].shortageQty == 6
    assert result["W-FIN-1.5-RED-90M"].shortageQty == 2
    assert result["MCB-HAV-SP-32A-C"].shortageQty == 0
