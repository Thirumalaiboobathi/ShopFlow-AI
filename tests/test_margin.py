"""Margin protection.

A confirmed supplier increase is only half a fact. The half that matters to the
owner is what it did to the profit on the product, and these tests pin that
down.

Two rules run through everything here:

  1. Nothing is invented. A missing cost produces an explicit unavailable
     state, never a plausible-looking substitute.
  2. Nothing is changed. The selling price is not written, the inventory is not
     touched, and the quotation total does not move. The last test in this file
     checks that against the real seeded shop rather than taking it on trust.

The canonical figures are derived from the seeded catalogue at the top of the
file, never typed in. If the seed moves, these tests move with it.
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from engine.loader import cached_dataset
from engine.margin import (
    HEALTHY,
    LOW_MARGIN,
    MARGIN_REDUCED,
    MARGIN_WARNING_PERCENT,
    NEGATIVE_MARGIN,
    NO_CONFIRMED_COST,
    NO_PREVIOUS_COST,
    NO_SELLING_PRICE,
    NO_SUCH_SKU,
    UNAVAILABLE,
    margin_alerts,
    margin_view,
    quotation_margin_impact,
    suggested_selling_price,
)
from engine.models import SupplierPrice
from engine.pricing import current_cost
from engine.quote import calculate_quote

# The canonical scenario, read from the shop rather than asserted about it.
WIRE = "W-FIN-1.5-RED-90M"
CONFIRMED_WIRE_COST = 6300.0


@pytest.fixture(scope="module")
def shop():
    return cached_dataset()


def simple(selling, previous, supplier="S-FAST"):
    """A one-product shop with exactly one recorded supplier price."""
    product = make_product("SKU-1", cost=previous, selling=selling)
    return make_dataset(
        [product],
        prices={"SKU-1": [SupplierPrice("SKU-1", supplier, "2026-01-01", previous)]},
    )


# ---------------------------------------------------------------------------
# 1-5  the arithmetic
# ---------------------------------------------------------------------------

def test_1_a_normal_margin_is_reported_and_flagged_healthy():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "SKU-1", 60.0)

    assert view["available"] is True
    assert view["oldMarginAmount"] == 40.0
    assert view["newMarginAmount"] == 40.0
    assert view["oldMarginPercent"] == 40.0
    assert view["marginReductionAmount"] == 0.0
    assert view["status"] == HEALTHY
    # Nothing changed, so there is nothing to recommend.
    assert view["suggestedSellingPrice"] is None


def test_2_a_price_increase_reduces_the_margin_by_exactly_the_increase():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "SKU-1", 70.0)

    assert view["oldMarginAmount"] == 40.0
    assert view["newMarginAmount"] == 30.0
    # The margin falls by the whole of the cost increase - the selling price
    # absorbs none of it, because nothing repriced the shelf.
    assert view["marginReductionAmount"] == pytest.approx(70.0 - 60.0)
    assert view["status"] == MARGIN_REDUCED


def test_3_a_price_decrease_is_never_reported_as_a_reduction():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "SKU-1", 50.0)

    assert view["newMarginAmount"] == 50.0
    assert view["marginReductionAmount"] < 0
    assert view["status"] == HEALTHY


def test_4_the_canonical_scenario_comes_out_of_the_seeded_shop(shop):
    """708 -> 308, down 400. Derived, not typed."""
    selling = shop.product(WIRE).sellingPrice
    previous = current_cost(shop, WIRE)

    view = margin_view(shop, WIRE, CONFIRMED_WIRE_COST)

    assert view["sellingPrice"] == selling
    assert view["previousSupplierCost"] == previous
    assert view["confirmedSupplierCost"] == CONFIRMED_WIRE_COST
    assert view["oldMarginAmount"] == pytest.approx(selling - previous)
    assert view["newMarginAmount"] == pytest.approx(selling - CONFIRMED_WIRE_COST)
    assert view["marginReductionAmount"] == pytest.approx(
        CONFIRMED_WIRE_COST - previous)
    # The percentages are the amounts over the selling price, nothing else.
    assert view["oldMarginPercent"] == pytest.approx(
        round((selling - previous) / selling * 100, 2))
    assert view["newMarginPercent"] == pytest.approx(
        round((selling - CONFIRMED_WIRE_COST) / selling * 100, 2))


def test_5_the_displayed_percentages_agree_with_the_displayed_reduction(shop):
    """A panel that does not add up is worse than no panel."""
    view = margin_view(shop, WIRE, CONFIRMED_WIRE_COST)
    assert view["marginReductionPercent"] == pytest.approx(
        round(view["oldMarginPercent"] - view["newMarginPercent"], 2))


# ---------------------------------------------------------------------------
# 5-6  thresholds
# ---------------------------------------------------------------------------

def test_6_a_thin_margin_is_flagged_low_against_the_configured_threshold():
    data = simple(selling=100.0, previous=88.0)
    view = margin_view(data, "SKU-1", 95.0, warning_percent=10.0)

    assert view["newMarginPercent"] == 5.0
    assert view["status"] == LOW_MARGIN
    assert view["marginWarningPercent"] == 10.0


def test_6b_the_threshold_is_configurable_and_changes_only_the_status():
    data = simple(selling=100.0, previous=88.0)
    strict = margin_view(data, "SKU-1", 95.0, warning_percent=10.0)
    lenient = margin_view(data, "SKU-1", 95.0, warning_percent=1.0)

    assert strict["status"] == LOW_MARGIN
    assert lenient["status"] == MARGIN_REDUCED
    # Only the classification moved. Every figure is identical.
    for field in ("oldMarginAmount", "newMarginAmount", "marginReductionAmount",
                  "oldMarginPercent", "newMarginPercent"):
        assert strict[field] == lenient[field]


def test_6c_the_default_threshold_flags_nothing_in_the_healthy_seeded_shop(shop):
    """The default is a warning line, not a business assumption in disguise."""
    for sku_id in shop.products:
        view = margin_view(shop, sku_id, current_cost(shop, sku_id))
        assert view["status"] == HEALTHY, (
            f"{sku_id} is flagged before any price change, so "
            f"MARGIN_WARNING_PERCENT={MARGIN_WARNING_PERCENT} is too high"
        )


def test_7_selling_below_cost_is_reported_as_a_negative_margin():
    data = simple(selling=100.0, previous=90.0)
    view = margin_view(data, "SKU-1", 120.0)

    assert view["newMarginAmount"] == -20.0
    assert view["status"] == NEGATIVE_MARGIN
    # Negative outranks low: the owner is losing money, not merely earning
    # little, and the panel must say the worse of the two things.
    assert view["status"] != LOW_MARGIN


# ---------------------------------------------------------------------------
# 8-10  missing and malformed data
# ---------------------------------------------------------------------------

def test_8_a_zero_selling_price_is_unavailable_not_a_division_by_zero():
    data = simple(selling=0.0, previous=60.0)
    view = margin_view(data, "SKU-1", 70.0)

    assert view["available"] is False
    assert view["status"] == UNAVAILABLE
    assert view["unavailableReason"] == NO_SELLING_PRICE
    assert view["oldMarginPercent"] is None


def test_9_a_missing_previous_cost_is_explicit_and_never_inferred():
    """No price history and no catalogue cost means no before-figure at all."""
    product = make_product("SKU-1", cost=0.0, selling=100.0)
    data = make_dataset([product], prices={})

    view = margin_view(data, "SKU-1", 70.0)

    assert view["available"] is False
    assert view["unavailableReason"] == NO_PREVIOUS_COST
    # The confirmed cost is NOT quietly promoted into the previous-cost slot.
    assert view["previousSupplierCost"] is None
    assert view["newMarginAmount"] is None


def test_10_a_missing_confirmed_cost_reports_the_current_margin_and_no_comparison():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "SKU-1", None)

    assert view["available"] is True            # the current margin is a fact
    assert view["comparisonAvailable"] is False  # but there is nothing to compare
    assert view["unavailableReason"] == NO_CONFIRMED_COST
    assert view["oldMarginAmount"] == 40.0
    assert view["newMarginAmount"] is None
    assert view["marginReductionAmount"] is None
    assert view["suggestedSellingPrice"] is None


def test_10b_malformed_costs_are_absent_rather_than_coerced():
    data = simple(selling=100.0, previous=60.0)
    for bad in ("", "abc", -5, 0, True, float("nan"), float("inf"), [], {}):
        view = margin_view(data, "SKU-1", bad)
        assert view["comparisonAvailable"] is False, bad
        assert view["newMarginAmount"] is None, bad


def test_10c_an_unknown_sku_is_refused():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "NOT-A-SKU", 70.0)
    assert view["unavailableReason"] == NO_SUCH_SKU
    assert view["available"] is False


# ---------------------------------------------------------------------------
# 11-12  the suggestion
# ---------------------------------------------------------------------------

def test_11_the_suggested_price_restores_the_reported_margin_percentage():
    data = simple(selling=100.0, previous=60.0)
    view = margin_view(data, "SKU-1", 70.0)

    target = view["suggestedSellingPrice"]
    assert target is not None
    # Selling at the target would earn back the percentage that was reported.
    restored = round((target - 70.0) / target * 100, 2)
    assert restored == pytest.approx(view["oldMarginPercent"], abs=0.01)


def test_11b_the_canonical_suggestion_is_derived_from_the_formula(shop):
    view = margin_view(shop, WIRE, CONFIRMED_WIRE_COST)
    expected = suggested_selling_price(
        CONFIRMED_WIRE_COST, view["oldMarginPercent"])
    assert view["suggestedSellingPrice"] == expected
    # It is above the new cost, or it would not be a suggestion at all.
    assert view["suggestedSellingPrice"] > CONFIRMED_WIRE_COST


def test_11c_no_suggestion_where_there_was_no_margin_to_restore():
    """A product already sold at a loss has no healthy position to return to."""
    data = simple(selling=100.0, previous=110.0)
    view = margin_view(data, "SKU-1", 130.0)
    assert view["suggestedSellingPrice"] is None
    assert suggested_selling_price(130.0, -10.0) is None
    assert suggested_selling_price(130.0, 100.0) is None


def test_12_rupee_figures_are_rounded_to_paise_and_percentages_to_two_places():
    data = simple(selling=333.33, previous=111.11)
    view = margin_view(data, "SKU-1", 222.22)

    for field in ("sellingPrice", "previousSupplierCost", "confirmedSupplierCost",
                  "oldMarginAmount", "newMarginAmount", "marginReductionAmount",
                  "suggestedSellingPrice"):
        value = view[field]
        if value is None:
            continue
        assert round(value, 2) == value, f"{field} carries sub-paise drift"
    for field in ("oldMarginPercent", "newMarginPercent", "marginReductionPercent"):
        assert round(view[field], 2) == view[field]


# ---------------------------------------------------------------------------
# 13  quotation impact
# ---------------------------------------------------------------------------

def test_13_a_confirmed_change_on_a_quoted_sku_is_reported_against_the_quotation(shop):
    quote = calculate_quote(shop, [(WIRE, 3)]).as_dict()
    alerts = margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST})

    impact = quotation_margin_impact(quote, alerts)

    assert impact["affectedCount"] == 1
    assert impact["affected"][0]["skuId"] == WIRE
    assert "affects this quotation" in impact["note"]


def test_13b_a_change_on_an_unquoted_sku_does_not_touch_the_quotation(shop):
    other = next(s for s in shop.products if s != WIRE)
    quote = calculate_quote(shop, [(other, 1)]).as_dict()
    alerts = margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST})

    impact = quotation_margin_impact(quote, alerts)

    assert impact["affectedCount"] == 0
    assert impact["affected"] == []
    assert "No confirmed supplier price change" in impact["note"]


def test_13c_the_quotation_total_is_not_changed_by_a_confirmed_cost(shop):
    """The customer's total is what it was. This is the whole rule of Phase 1."""
    before = calculate_quote(shop, [(WIRE, 3)]).as_dict()
    alerts = margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST})
    impact = quotation_margin_impact(before, alerts)
    after = calculate_quote(shop, [(WIRE, 3)]).as_dict()

    assert after["total"] == before["total"]
    assert impact["quotationTotalChanged"] is False
    # The total is deliberately not even carried in the impact payload, so no
    # UI can redisplay it as though it had moved.
    assert "total" not in impact


# ---------------------------------------------------------------------------
# 14  the safety property
# ---------------------------------------------------------------------------

def test_14_the_selling_price_is_never_mutated_by_anything_in_this_module(shop):
    prices_before = {s: p.sellingPrice for s, p in shop.products.items()}
    costs_before = {s: current_cost(shop, s) for s in shop.products}
    stock_before = {s: shop.onHand(s) for s in shop.products}

    margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST, "NOT-A-SKU": 10.0})
    margin_view(shop, WIRE, CONFIRMED_WIRE_COST)
    quotation_margin_impact(
        calculate_quote(shop, [(WIRE, 3)]).as_dict(),
        margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST}),
    )

    assert {s: p.sellingPrice for s, p in shop.products.items()} == prices_before
    assert {s: current_cost(shop, s) for s in shop.products} == costs_before
    assert {s: shop.onHand(s) for s in shop.products} == stock_before


def test_14b_every_view_states_that_the_selling_price_was_not_changed(shop):
    for view in margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST}):
        assert view["sellingPriceChanged"] is False
        assert "does not automatically change your selling price" in view["note"]


def test_14c_the_module_cannot_write_anything():
    """Structural, not behavioural: there is no route to a store from here."""
    import inspect

    from engine import margin

    source = inspect.getsource(margin)
    for forbidden in ("boto3", "put_item", "update_item", "delete_item",
                      "open(", "requests"):
        assert forbidden not in source, f"margin.py reaches for {forbidden}"


def test_14d_alerts_are_ordered_by_how_much_margin_was_lost(shop):
    other = next(
        s for s in shop.products
        if s != WIRE and current_cost(shop, s) > 0
    )
    costs = {
        WIRE: CONFIRMED_WIRE_COST,
        other: current_cost(shop, other) + 1.0,
    }
    alerts = margin_alerts(shop, costs)
    reductions = [a["marginReductionAmount"] for a in alerts]
    assert reductions == sorted(reductions, reverse=True)


def test_14e_an_unavailable_product_is_kept_in_the_alerts_not_dropped(shop):
    """Silently omitting a product would read as an all-clear."""
    alerts = margin_alerts(shop, {WIRE: CONFIRMED_WIRE_COST, "NOT-A-SKU": 99.0})
    assert {a["skuId"] for a in alerts} == {WIRE, "NOT-A-SKU"}
    unavailable = next(a for a in alerts if a["skuId"] == "NOT-A-SKU")
    assert unavailable["available"] is False
