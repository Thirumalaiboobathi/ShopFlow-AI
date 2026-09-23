"""Inventory lookup, shortage calculation and quotation totals."""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from engine.quote import (
    DuplicateSkuError,
    InvalidQuantityError,
    UnknownSkuError,
    calculate_quote,
    check_inventory,
)


def test_quote_total_is_the_sum_of_price_times_quantity():
    products = [make_product("A", 100, 250), make_product("B", 100, 125.5)]
    data = make_dataset(products, inventory={"A": 10, "B": 10},
                        velocity={"A": 1, "B": 1})
    quote = calculate_quote(data, [("A", 3), ("B", 2)])
    assert quote.lines[0].lineTotal == 750.0
    assert quote.lines[1].lineTotal == 251.0
    assert quote.total == 1001.0


def test_quote_reports_shortage_against_stock():
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 14}, velocity={"A": 5})
    line = calculate_quote(data, [("A", 20)]).lines[0]
    assert line.onHand == 14
    assert line.shortageQty == 6
    assert line.inStock is False


def test_quote_marks_a_covered_line_in_stock():
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 5}, velocity={"A": 1})
    line = calculate_quote(data, [("A", 2)]).lines[0]
    assert line.shortageQty == 0
    assert line.as_dict()["inStock"] is True


def test_unknown_sku_is_rejected_not_silently_dropped():
    data = make_dataset([make_product("A", 100, 150)])
    with pytest.raises(UnknownSkuError) as exc:
        calculate_quote(data, [("A", 1), ("SKU-INVENTED-BY-MODEL", 2)])
    assert exc.value.skuIds == ["SKU-INVENTED-BY-MODEL"]


def test_zero_and_negative_quantities_are_rejected():
    data = make_dataset([make_product("A", 100, 150)])
    for bad in (0, -3):
        with pytest.raises(InvalidQuantityError):
            calculate_quote(data, [("A", bad)])


def test_non_integer_quantity_is_rejected():
    data = make_dataset([make_product("A", 100, 150)])
    for bad in (1.5, "20", None, True):
        with pytest.raises(InvalidQuantityError):
            calculate_quote(data, [("A", bad)])


def test_repeated_sku_lines_are_refused_not_added_together():
    """This used to merge 3 + 2 into 5. Adding lines together is how one
    duplicated model call became a quotation for four breakers instead of two:
    a quotation lists each SKU once, and anything else is refused."""
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 100}, velocity={"A": 1})
    with pytest.raises(DuplicateSkuError) as exc:
        calculate_quote(data, [("A", 3), ("A", 2)])
    assert exc.value.skuIds == ["A"]


def test_inventory_lookup_returns_deterministic_stock():
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 21}, velocity={"A": 7})
    row = check_inventory(data, ["A"])[0]
    assert row["onHand"] == 21
    assert row["weeklyVelocity"] == 7.0
    assert row["coverageWeeks"] == 3.0
    assert row["supplierLeadTimeDays"] == 7


def test_inventory_lookup_rejects_unknown_skus():
    data = make_dataset([make_product("A", 100, 150)])
    with pytest.raises(UnknownSkuError):
        check_inventory(data, ["A", "NOPE"])


def test_dead_stock_reports_no_coverage_rather_than_infinity():
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 9}, velocity={"A": 0})
    assert check_inventory(data, ["A"])[0]["coverageWeeks"] is None


def test_quote_carries_evidence_for_every_line():
    data = make_dataset([make_product("A", 100, 150)],
                        inventory={"A": 1}, velocity={"A": 2})
    line = calculate_quote(data, [("A", 3)]).lines[0].as_dict()
    assert line["evidence"]["lineTotal"] == "3 x 150 = 450.0"
    assert line["evidence"]["shortage"] == "ordered 3, 1 in stock, short 2"


# ---- the canonical order, priced by the engine ----

def test_canonical_order_totals_match_the_seeded_scenario(seeded):
    quote = calculate_quote(seeded, [
        ("SW-ANC-1W10A", 20),
        ("W-FIN-1.5-RED-90M", 3),
        ("MCB-HAV-SP-32A-C", 2),
    ])
    assert quote.total == 22306.48
    shortages = {l.skuId: l.shortageQty for l in quote.lines}
    assert shortages == {
        "SW-ANC-1W10A": 6,
        "W-FIN-1.5-RED-90M": 2,
        "MCB-HAV-SP-32A-C": 0,
    }


def test_canonical_quote_agrees_with_the_demo_scenario(seeded):
    """Two paths to the same number, so neither can drift alone."""
    from data.demo_scenario import build_scenario

    scenario = build_scenario(seeded)
    quote = calculate_quote(seeded, [
        (l["skuId"], l["quantity"]) for l in scenario["order"]["lines"]
    ])
    assert quote.total == scenario["order"]["quoteTotal"]
