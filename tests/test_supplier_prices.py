"""Supplier price intelligence.

The business claim under test: when a supplier's price moves materially, the
owner is told, shown the arithmetic, and asked - and nothing changes until
they say so.
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from engine.models import SupplierPrice
from engine.pricing import PRICE_ALERT_THRESHOLD_PERCENT, current_cost
from engine.supplier_prices import (
    AMBIGUOUS,
    CONFIRMED,
    DECREASE,
    INCREASE,
    MATCHED,
    REJECTED,
    UNCHANGED,
    UNMATCHED,
    InvalidSupplierLineError,
    SupplierLine,
    build_decision_record,
    build_supplier_line,
    compare_price,
    match_supplier_line,
    review_price_list,
)


def priced(sku, cost, selling=None, supplier="S-FAST"):
    return make_product(sku, cost, selling or cost * 1.3, supplierId=supplier)


# ---- 2. strict extraction schema validation ----

def test_valid_line_is_accepted():
    line = build_supplier_line({
        "description": "Finolex 1.5 sqmm Red 90m", "price": 6300,
        "brand": "Finolex", "colour": "Red", "length": "90m"})
    assert line.price == 6300.0
    assert line.brand == "Finolex"


def test_line_without_a_description_is_rejected():
    with pytest.raises(InvalidSupplierLineError):
        build_supplier_line({"price": 100})


def test_line_without_a_price_is_rejected():
    with pytest.raises(InvalidSupplierLineError):
        build_supplier_line({"description": "wire"})


def test_price_as_a_string_is_rejected_not_coerced():
    """A model that returns "6,300" must fail loudly, not be repaired."""
    with pytest.raises(InvalidSupplierLineError):
        build_supplier_line({"description": "wire", "price": "6300"})


def test_non_positive_price_is_rejected():
    for bad in (0, -5):
        with pytest.raises(InvalidSupplierLineError):
            build_supplier_line({"description": "wire", "price": bad})


def test_boolean_price_is_rejected():
    with pytest.raises(InvalidSupplierLineError):
        build_supplier_line({"description": "wire", "price": True})


def test_blank_optional_fields_become_none():
    line = build_supplier_line({"description": "wire", "price": 10,
                                "brand": "   ", "colour": None})
    assert line.brand is None and line.colour is None


def test_empty_document_is_rejected():
    data = make_dataset([priced("A", 100)])
    with pytest.raises(InvalidSupplierLineError):
        review_price_list(data, "S", None, [])


def test_absurdly_long_document_is_rejected():
    data = make_dataset([priced("A", 100)])
    rows = [{"description": "x", "price": 1} for _ in range(51)]
    with pytest.raises(InvalidSupplierLineError):
        review_price_list(data, "S", None, rows)


# ---- 3/4/5. matching against the real catalogue only ----

def test_supplier_line_matches_an_existing_sku(seeded):
    line = SupplierLine(description="Finolex 1.5 sqmm FR Wire RED 90m coil",
                        price=6300.0, brand="Finolex",
                        specification="1.5 sqmm", colour="Red", length="90m")
    result = match_supplier_line(seeded, line)
    assert result.status == MATCHED
    assert result.skuId == "W-FIN-1.5-RED-90M"


def test_ambiguous_supplier_line_is_not_silently_resolved(seeded):
    """Same rule as customer orders: several matches means a question."""
    line = SupplierLine(description="Finolex 1.5 sqmm FR Wire 90m coil",
                        price=6300.0, brand="Finolex",
                        specification="1.5 sqmm", length="90m")
    result = match_supplier_line(seeded, line)
    assert result.status == AMBIGUOUS
    assert result.skuId is None
    assert result.comparison is None
    assert result.clarifyingAttribute == "colour"
    assert len(result.candidates) > 1


def test_unknown_product_is_unmatched_not_invented(seeded):
    line = SupplierLine(description="Kaveri 4-core Armoured Cable 25 sqmm",
                        price=18450.0, brand="Kaveri")
    result = match_supplier_line(seeded, line)
    assert result.status == UNMATCHED
    assert result.skuId is None
    assert result.comparison is None


def test_every_matched_sku_exists_in_the_catalog(seeded):
    review = review_price_list(seeded, "Sri Balaji", "2026-09-15", [
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil", "price": 6300,
         "brand": "Finolex", "specification": "1.5 sqmm", "colour": "Red",
         "length": "90m"},
        {"description": "Kaveri Armoured Cable", "price": 100, "brand": "Kaveri"},
    ])
    for result in review.matched:
        assert result.skuId in seeded.products


# ---- 6/7/8. price arithmetic ----

def test_price_increase_is_calculated_deterministically():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 5900.0)]})
    c = compare_price(data, "A", 6300.0)
    assert c.previousPrice == 5900.0
    assert c.absoluteDelta == 400.0
    assert c.percentageDelta == pytest.approx(6.78, abs=0.01)
    assert c.direction == INCREASE
    assert c.materialChange is True


def test_price_decrease_is_calculated_and_flagged():
    """A sharp fall matters too - it changes what the next purchase costs."""
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 1000.0)]})
    c = compare_price(data, "A", 880.0)
    assert c.absoluteDelta == -120.0
    assert c.percentageDelta == pytest.approx(-12.0)
    assert c.direction == DECREASE
    assert c.materialChange is True


def test_unchanged_price_reports_no_movement():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 358.0)]})
    c = compare_price(data, "A", 358.0)
    assert c.absoluteDelta == 0.0
    assert c.direction == UNCHANGED
    assert c.materialChange is False


def test_first_ever_quote_is_not_reported_as_a_rise():
    data = make_dataset([make_product("A", 0.0, 10.0)])
    c = compare_price(data, "A", 500.0)
    assert c.previousPrice is None
    assert c.percentageDelta is None
    assert c.absoluteDelta is None
    assert c.materialChange is False
    assert "no previously recorded price" in c.as_dict()["evidence"]["calculation"]


def test_comparing_an_unknown_sku_raises():
    data = make_dataset([priced("A", 100)])
    with pytest.raises(ValueError):
        compare_price(data, "NOT-A-SKU", 100.0)


# ---- 9/10. the 5% threshold ----

def test_default_threshold_is_five_percent():
    assert PRICE_ALERT_THRESHOLD_PERCENT == 5.0


def test_change_below_the_threshold_is_not_material():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0)]})
    c = compare_price(data, "A", 104.0)          # +4%
    assert c.percentageDelta == pytest.approx(4.0)
    assert c.materialChange is False


def test_change_above_the_threshold_is_material():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0)]})
    assert compare_price(data, "A", 106.0).materialChange is True


def test_change_exactly_at_the_threshold_is_not_material():
    """The rule is "more than 5%", and the boundary is pinned so it cannot
    drift into or out of alerting unnoticed."""
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0)]})
    c = compare_price(data, "A", 105.0)
    assert c.percentageDelta == pytest.approx(5.0)
    assert c.materialChange is False


def test_threshold_is_configurable():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 100.0)]})
    assert compare_price(data, "A", 102.0, threshold=1.0).materialChange is True
    assert compare_price(data, "A", 102.0, threshold=10.0).materialChange is False


# ---- 11. the planted +6.78% change, end to end ----

def test_planted_finolex_change_is_detected_from_the_seeded_price(seeded):
    """The shop's record says 5,900. The price list says 6,300."""
    assert current_cost(seeded, "W-FIN-1.5-RED-90M") == 5900.0

    review = review_price_list(seeded, "Sri Balaji Electricals", "2026-09-15", [
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil", "price": 6300.0,
         "brand": "Finolex", "specification": "1.5 sqmm", "colour": "Red",
         "length": "90m"},
    ])
    line = review.matched[0]
    assert line.skuId == "W-FIN-1.5-RED-90M"
    c = line.comparison
    assert c.previousPrice == 5900.0
    assert c.currentPrice == 6300.0
    assert c.absoluteDelta == 400.0
    assert c.percentageDelta == pytest.approx(6.78, abs=0.01)
    assert c.materialChange is True
    assert len(review.materialChanges) == 1


def test_canonical_price_list_produces_one_of_each_status(seeded):
    """Matched-and-material, matched-and-minor, unchanged, ambiguous, unmatched."""
    review = review_price_list(seeded, "Sri Balaji Electricals", "2026-09-15", [
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil", "price": 6300.0,
         "brand": "Finolex", "specification": "1.5 sqmm", "colour": "Red",
         "length": "90m"},
        {"description": "Anchor Modular Switch 1-Way 10A White", "price": 58.99,
         "brand": "Anchor", "specification": "1-Way 10A"},
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
         "brand": "Havells", "specification": "SP 32A"},
        {"description": "Finolex 1.5 sqmm FR Wire 90m coil", "price": 6300.0,
         "brand": "Finolex", "specification": "1.5 sqmm", "length": "90m"},
        {"description": "Kaveri 4-core Armoured Cable 25 sqmm", "price": 18450.0,
         "brand": "Kaveri"},
    ])
    payload = review.as_dict()
    assert payload["lineCount"] == 5
    assert payload["matchedCount"] == 3
    assert payload["ambiguousCount"] == 1
    assert payload["unmatchedCount"] == 1
    assert payload["materialChangeCount"] == 1


# ---- 15. evidence ----

def test_comparison_carries_the_arithmetic_that_produced_it():
    data = make_dataset([priced("A", 100)], prices={
        "A": [SupplierPrice("A", "S-FAST", "2026-07-01", 5900.0)]})
    evidence = compare_price(data, "A", 6300.0).as_dict()["evidence"]
    assert evidence["calculation"] == "(6300.0 - 5900.0) / 5900.0 x 100 = 6.78%"
    assert evidence["absolute"] == "6300.0 - 5900.0 = 400.0"
    assert "5.0%" in evidence["threshold"]
    assert evidence["source"] == "engine.supplier_prices.compare_price"


# ---- 12/13/14. owner decisions ----

def test_owner_can_confirm_a_change(seeded):
    record = build_decision_record(
        seeded, "job1", "W-FIN-1.5-RED-90M", CONFIRMED,
        {"previousPrice": 5900.0, "currentPrice": 6300.0, "percentageDelta": 6.78})
    assert record["decision"] == CONFIRMED
    assert record["skuId"] == "W-FIN-1.5-RED-90M"
    assert record["percentageDelta"] == 6.78


def test_owner_can_reject_a_change(seeded):
    record = build_decision_record(
        seeded, "job1", "W-FIN-1.5-RED-90M", REJECTED,
        {"previousPrice": 5900.0, "currentPrice": 6300.0, "percentageDelta": 6.78})
    assert record["decision"] == REJECTED


def test_an_invalid_decision_is_rejected(seeded):
    with pytest.raises(ValueError):
        build_decision_record(seeded, "job1", "W-FIN-1.5-RED-90M", "MAYBE", {})


def test_a_decision_cannot_name_a_sku_outside_the_catalog(seeded):
    with pytest.raises(ValueError):
        build_decision_record(seeded, "job1", "INVENTED-SKU", CONFIRMED, {})


def test_confirming_a_change_does_not_move_the_catalog_price(seeded):
    """Confirmation records agreement. It does not rewrite the shop's costs -
    that is a separate step the owner triggers knowingly."""
    before = current_cost(seeded, "W-FIN-1.5-RED-90M")
    catalog_before = seeded.product("W-FIN-1.5-RED-90M").costPrice

    record = build_decision_record(
        seeded, "job1", "W-FIN-1.5-RED-90M", CONFIRMED,
        {"previousPrice": 5900.0, "currentPrice": 6300.0, "percentageDelta": 6.78})

    assert record["catalogPriceChanged"] is False
    assert current_cost(seeded, "W-FIN-1.5-RED-90M") == before
    assert seeded.product("W-FIN-1.5-RED-90M").costPrice == catalog_before


def test_reviewing_a_price_list_never_mutates_the_dataset(seeded):
    before = {s: current_cost(seeded, s) for s in seeded.products}
    review_price_list(seeded, "Sri Balaji", "2026-09-15", [
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil", "price": 9999.0,
         "brand": "Finolex", "specification": "1.5 sqmm", "colour": "Red",
         "length": "90m"},
    ])
    assert {s: current_cost(seeded, s) for s in seeded.products} == before
