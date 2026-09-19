"""Supplier cost, price-change detection and competing quotes."""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from engine.models import SupplierPrice
from engine.pricing import (
    cheaper_alternatives,
    current_cost,
    detect_price_increases,
    margin_per_rupee,
    price_delta,
)


def hist(*entries):
    return [SupplierPrice("A", sup, day, cost) for sup, day, cost in entries]


def test_price_delta_reports_absolute_and_percent_change():
    data = make_dataset([make_product("A", 100, 150)], prices={
        "A": hist(("S-FAST", "2026-07-01", 5900.0), ("S-FAST", "2026-09-05", 6300.0))
    })
    d = price_delta(data, "A")
    assert d.previousCost == 5900.0
    assert d.currentCost == 6300.0
    assert d.absoluteChange == 400.0
    assert d.percentChange == pytest.approx(6.78, abs=0.01)
    assert d.increased is True


def test_price_delta_compares_only_the_two_most_recent_prices():
    data = make_dataset([make_product("A", 100, 150)], prices={
        "A": hist(("S-FAST", "2026-01-01", 100.0), ("S-FAST", "2026-05-01", 200.0),
                  ("S-FAST", "2026-09-01", 220.0))
    })
    assert price_delta(data, "A").percentChange == pytest.approx(10.0)


def test_price_delta_is_none_without_history():
    data = make_dataset([make_product("A", 100, 150)])
    assert price_delta(data, "A") is None


def test_first_ever_price_has_no_percent_change():
    data = make_dataset([make_product("A", 100, 150)], prices={
        "A": hist(("S-FAST", "2026-09-01", 120.0))
    })
    d = price_delta(data, "A")
    assert d.previousCost is None
    assert d.percentChange is None
    assert d.increased is False


def test_current_cost_falls_back_to_catalog_price():
    data = make_dataset([make_product("A", 100, 150)])
    assert current_cost(data, "A") == 100.0


def test_rival_quote_does_not_become_the_shops_cost_basis():
    # A cheaper quote from another supplier is an option, not the current cost.
    data = make_dataset([make_product("A", 100, 150, supplierId="S-FAST")], prices={
        "A": hist(("S-FAST", "2026-09-01", 400.0), ("S-SLOW", "2026-09-05", 300.0))
    })
    assert current_cost(data, "A") == 400.0


def test_rival_quote_does_not_distort_price_delta():
    data = make_dataset([make_product("A", 100, 150, supplierId="S-FAST")], prices={
        "A": hist(("S-FAST", "2026-07-01", 380.0), ("S-FAST", "2026-09-01", 400.0),
                  ("S-SLOW", "2026-09-05", 300.0))
    })
    d = price_delta(data, "A")
    assert (d.previousCost, d.currentCost) == (380.0, 400.0)


def test_cheaper_alternative_is_detected_with_saving():
    data = make_dataset([make_product("A", 100, 150, supplierId="S-FAST")], prices={
        "A": hist(("S-FAST", "2026-09-01", 400.0), ("S-SLOW", "2026-09-05", 300.0))
    })
    alts = cheaper_alternatives(data, "A")
    assert len(alts) == 1
    assert alts[0].supplierId == "S-SLOW"
    assert alts[0].saving == 100.0
    assert alts[0].savingPercent == pytest.approx(25.0)


def test_more_expensive_rival_is_not_reported():
    data = make_dataset([make_product("A", 100, 150, supplierId="S-FAST")], prices={
        "A": hist(("S-FAST", "2026-09-01", 300.0), ("S-SLOW", "2026-09-05", 400.0))
    })
    assert cheaper_alternatives(data, "A") == []


def test_detect_price_increases_respects_threshold_and_sorts_by_severity():
    products = [make_product("A", 100, 150), make_product("B", 100, 150)]
    data = make_dataset(products, prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0),
              SupplierPrice("A", "S-FAST", "2026-09-01", 102.0)],   # +2%
        "B": [SupplierPrice("B", "S-FAST", "2026-07-01", 100.0),
              SupplierPrice("B", "S-FAST", "2026-09-01", 110.0)],   # +10%
    })
    found = detect_price_increases(data, threshold_percent=5.0)
    assert [d.skuId for d in found] == ["B"]
    assert [d.skuId for d in detect_price_increases(data, 1.0)] == ["B", "A"]


def test_margin_per_rupee_uses_live_cost_not_catalog_cost():
    # A price rise must erode margin immediately, or the allocator over-buys.
    data = make_dataset([make_product("A", cost=100, selling=150)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-09-01", 125.0)]
    })
    assert margin_per_rupee(data, "A") == pytest.approx(0.2)


def test_margin_per_rupee_is_zero_when_cost_is_zero():
    data = make_dataset([make_product("A", cost=0.0, selling=150)])
    assert margin_per_rupee(data, "A") == 0.0


def test_seeded_wire_price_increase_is_the_planted_one(seeded):
    d = price_delta(seeded, "W-FIN-1.5-RED-90M")
    assert (d.previousCost, d.currentCost) == (5900.0, 6300.0)
    assert d.percentChange == pytest.approx(6.78, abs=0.01)
