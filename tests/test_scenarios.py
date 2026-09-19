"""The planted business conditions must actually be present in the seed."""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from data.generator import (
    CHEAPER_ALTERNATIVE_SKU,
    DEAD_STOCK_SKU,
    PRICE_INCREASE_SKU,
    SLOW_MOVING_SKU,
    build_dataset,
)
from engine.scenarios import (
    ambiguity_groups,
    cheaper_supplier_options,
    committed_shortages,
    dead_stock,
    imminent_stockouts,
    slow_moving,
    supplier_price_increases,
)


def test_catalog_is_about_150_skus(seeded):
    assert 140 <= len(seeded.products) <= 160


def test_every_product_has_inventory_and_a_known_supplier(seeded):
    assert set(seeded.inventory) == set(seeded.products)
    for p in seeded.products.values():
        assert p.supplierId in seeded.suppliers


def test_every_product_has_six_months_of_weekly_sales(seeded):
    for skuId in seeded.products:
        assert len(seeded.weeklySales(skuId)) == 26


def test_selling_price_always_exceeds_cost(seeded):
    for p in seeded.products.values():
        assert p.sellingPrice > p.costPrice


def test_generator_is_deterministic():
    first, second = build_dataset(), build_dataset()
    assert first.products == second.products
    assert first.inventory == second.inventory
    assert first.sales == second.sales
    assert first.priceHistory == second.priceHistory


# ---- planted scenarios ----

def test_imminent_stockout_is_planted(seeded):
    found = {f.skuId for f in imminent_stockouts(seeded)}
    assert "W-FIN-1.5-RED-90M" in found


def test_dead_stock_is_planted(seeded):
    found = {f.skuId for f in dead_stock(seeded)}
    assert DEAD_STOCK_SKU in found


def test_slow_moving_stock_is_planted(seeded):
    found = {f.skuId for f in slow_moving(seeded)}
    assert SLOW_MOVING_SKU in found


def test_dead_stock_and_slow_moving_are_disjoint(seeded):
    dead = {f.skuId for f in dead_stock(seeded)}
    slow = {f.skuId for f in slow_moving(seeded)}
    assert dead.isdisjoint(slow)


def test_supplier_price_increase_is_planted(seeded):
    found = {d.skuId for d in supplier_price_increases(seeded, threshold_percent=5.0)}
    assert PRICE_INCREASE_SKU in found


def test_cheaper_supplier_alternative_is_planted(seeded):
    found = {f.skuId for f in cheaper_supplier_options(seeded)}
    assert CHEAPER_ALTERNATIVE_SKU in found


def test_committed_customer_shortage_is_planted(seeded):
    short = {s.skuId for s in committed_shortages(seeded)}
    assert {"SW-ANC-1W10A", "W-FIN-1.5-RED-90M"} <= short


# ---- ambiguity ----

def test_finolex_15_wire_is_ambiguous_by_colour(seeded):
    """The core clarification case: colour is the only thing that differs."""
    colour_groups = [
        g for g in ambiguity_groups(seeded)
        if g.distinguishingAttribute == "colour"
        and g.shared.get("brand") == "Finolex"
        and g.shared.get("specification") == "1.5 sqmm"
        and g.shared.get("length") == "90m"
    ]
    assert len(colour_groups) == 1
    assert colour_groups[0].skuIds == [
        "W-FIN-1.5-BLK-90M", "W-FIN-1.5-BLU-90M", "W-FIN-1.5-RED-90M",
    ]


def test_finolex_15_red_wire_is_also_ambiguous_by_length(seeded):
    groups = [
        g for g in ambiguity_groups(seeded)
        if g.distinguishingAttribute == "length"
        and g.shared.get("brand") == "Finolex"
        and g.shared.get("colour") == "Red"
        and g.shared.get("specification") == "1.5 sqmm"
    ]
    assert len(groups) == 1
    assert set(groups[0].skuIds) == {"W-FIN-1.5-RED-90M", "W-FIN-1.5-RED-180M"}


def test_a_unique_product_is_not_reported_as_ambiguous():
    products = [
        make_product("ONLY", 100, 150, brand="B1", category="Wire",
                     specification="1.5 sqmm", colour="Red", length="90m"),
        make_product("OTHER", 100, 150, brand="B1", category="Switch",
                     specification="1-Way", colour="White", length=None),
    ]
    data = make_dataset(products)
    assert ambiguity_groups(data) == []


def test_ambiguity_requires_the_attribute_to_actually_differ():
    # Same colour twice is not an ambiguity, even with two SKUs in the group.
    products = [
        make_product("A", 100, 150, brand="B1", specification="1.5 sqmm",
                     colour="Red", length="90m"),
        make_product("B", 100, 150, brand="B1", specification="1.5 sqmm",
                     colour="Red", length="180m"),
    ]
    data = make_dataset(products)
    colour = [g for g in ambiguity_groups(data)
              if g.distinguishingAttribute == "colour"]
    assert colour == []
