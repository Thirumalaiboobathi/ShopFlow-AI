"""Units of measure.

The single rule worth testing here is that ShopFlow never converts an order
quantity behind the owner's back. Everything else - the plurals, the aliases,
the equivalence shown beside a line - exists to make that rule usable.

So the tests come in two halves. The first half checks that a stated unit is
understood. The second half checks what happens when it cannot be honoured,
and every one of those cases ends in a question rather than a number.

The conversions are read off the catalogue. Nothing in this file asserts that
a coil is 90 metres as a general fact; it asserts that the 90 m SKU says 90
and the 180 m SKU says 180.
"""

from __future__ import annotations

import pytest

from conftest import make_dataset, make_product
from engine.loader import cached_dataset
from engine.matching import NOT_FOUND, RESOLVED
from engine.matching import resolve_product
from engine.models import Product
from engine.quote import UomMismatchError, calculate_quote, check_inventory
from engine.uom import (
    BOX,
    COIL,
    LENGTH,
    METER,
    PACK,
    PIECE,
    STOCKING_UOMS,
    SUPPORTED_UOMS,
    UOM_CONVERTIBLE,
    UOM_MATCHES,
    UOM_NOT_RECONCILABLE,
    UOM_UNSPECIFIED,
    UnknownUomError,
    base_equivalent,
    conversion_note,
    has_conversion,
    normalize_uom,
    product_uom,
    require_uom,
    resolve_uom,
    split_quantity_and_uom,
)

WIRE_90 = "W-FIN-1.5-RED-90M"
WIRE_180 = "W-FIN-1.5-RED-180M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"
CONDUIT = "ACC-CONDUIT-20"
CLIPS = "ACC-CLIP-CLAMP"

CANONICAL = [(SWITCH, 20), (WIRE_90, 3), (MCB, 2)]


@pytest.fixture(scope="module")
def shop():
    return cached_dataset()


# ---------------------------------------------------------------------------
# 1-5  the units this catalogue actually uses
# ---------------------------------------------------------------------------

def test_1_pieces(shop):
    assert product_uom(shop.product(SWITCH)) == PIECE
    assert product_uom(shop.product(MCB)) == PIECE


def test_2_coils(shop):
    assert product_uom(shop.product(WIRE_90)) == COIL


def test_3_meter_is_a_base_unit_and_not_a_stocking_unit(shop):
    """No SKU is stocked by the metre, and METER is excluded from the units a
    product may be sold in. That exclusion is what stops a metre request
    quietly becoming a stock line."""
    assert METER in SUPPORTED_UOMS
    assert METER not in STOCKING_UOMS
    assert all(product_uom(p) != METER for p in shop.products.values())


def test_4_boxes_are_understood_but_nothing_is_stocked_in_them(shop):
    """BOX is a real unit the shop hears and does not use. Both halves matter:
    it must be recognised, and it must match nothing."""
    assert normalize_uom("boxes") == BOX
    assert all(product_uom(p) != BOX for p in shop.products.values())


def test_5_packs_and_lengths(shop):
    assert product_uom(shop.product(CLIPS)) == PACK
    assert product_uom(shop.product(CONDUIT)) == LENGTH


def test_5b_every_product_has_exactly_one_known_stocking_unit(shop):
    for sku, product in shop.products.items():
        assert product_uom(product) in STOCKING_UOMS, sku


# ---------------------------------------------------------------------------
# 6-7  plurals, aliases and normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("piece", PIECE), ("pieces", PIECE), ("pcs", PIECE), ("pc", PIECE),
    ("nos", PIECE), ("PIECE", PIECE),
    ("coil", COIL), ("coils", COIL), ("bundle", COIL), ("rolls", COIL),
    ("meter", METER), ("meters", METER), ("metre", METER), ("metres", METER),
    ("m", METER), ("mtr", METER),
    ("box", BOX), ("boxes", BOX), ("carton", BOX),
    ("pack", PACK), ("packs", PACK), ("packet", PACK),
    ("length", LENGTH), ("lengths", LENGTH), ("rod", LENGTH),
])
def test_6_plural_and_abbreviated_forms_normalise(word, expected):
    assert normalize_uom(word) == expected


@pytest.mark.parametrize("word", [
    "kandu", "sutru", "petti", "meetar",
])
def test_6b_tanglish_unit_words_map_onto_units_the_shop_uses(word):
    assert normalize_uom(word) in SUPPORTED_UOMS


@pytest.mark.parametrize("word", [
    "", None, "wire", "finolex", "kilogram", "kg", "litre", "dozen",
    "mootai", "bag", "tonne",
])
def test_7_a_word_that_is_not_a_unit_here_is_not_turned_into_one(word):
    """A dozen and a sack are real units. This shop does not sell in them, so
    reading one would be inventing a unit the catalogue cannot honour."""
    assert normalize_uom(word) is None


def test_7b_require_uom_raises_rather_than_guessing():
    assert require_uom("coils") == COIL
    with pytest.raises(UnknownUomError):
        require_uom("dozen")


def test_7c_quantity_and_unit_are_read_out_of_a_phrase():
    assert split_quantity_and_uom("3 coils Finolex 1.5 sq mm red wire") == {
        "quantity": 3.0, "uom": COIL, "matched": "3 coils"}
    assert split_quantity_and_uom("90 metres Finolex red wire")["uom"] == METER
    assert split_quantity_and_uom("2 boxes switches")["uom"] == BOX


def test_7d_a_specification_is_not_read_as_a_unit():
    """"20 Anchor switch 10 amp" states no unit. "amp" is a rating."""
    assert split_quantity_and_uom("20 Anchor modular switch 10 amp") is None
    assert split_quantity_and_uom("1.5 sq mm wire") is None


# ---------------------------------------------------------------------------
# 8-9  conversion, read from the catalogue
# ---------------------------------------------------------------------------

def test_8_a_valid_conversion_comes_from_the_sku(shop):
    ninety = shop.product(WIRE_90)
    assert has_conversion(ninety)
    assert conversion_note(ninety) == "1 COIL = 90 METER"

    equivalent = base_equivalent(ninety, 3)
    assert equivalent["quantity"] == 270.0
    assert equivalent["uom"] == METER
    assert equivalent["calculation"] == "3 COIL x 90 METER = 270 METER"


def test_8b_a_different_coil_declares_a_different_length(shop):
    """Nothing here assumes coils are 90 m. The 180 m SKU says 180."""
    assert conversion_note(shop.product(WIRE_180)) == "1 COIL = 180 METER"
    assert base_equivalent(shop.product(WIRE_180), 1)["quantity"] == 180.0


def test_9_a_sku_with_no_measurement_has_no_conversion(shop):
    """Conduit is sold by the length and clips by the pack. This shop has not
    recorded what those contain, so nothing is assumed."""
    for sku in (CONDUIT, CLIPS, SWITCH, MCB):
        product = shop.product(sku)
        assert not has_conversion(product), sku
        assert conversion_note(product) is None
        assert base_equivalent(product, 5) is None


def test_9b_exactly_the_coil_skus_carry_a_conversion(shop):
    with_conversion = {s for s, p in shop.products.items() if has_conversion(p)}
    coils = {s for s, p in shop.products.items() if product_uom(p) == COIL}
    assert with_conversion == coils
    assert len(coils) == 35


# ---------------------------------------------------------------------------
# 10-12  reconciling a stated unit with a SKU
# ---------------------------------------------------------------------------

def test_10_no_stated_unit_is_not_a_problem(shop):
    resolution = resolve_uom(shop.product(WIRE_90), None)
    assert resolution["status"] == UOM_UNSPECIFIED
    assert resolution["needsClarification"] is False
    assert resolution["catalogueUom"] == COIL


def test_10b_the_catalogue_unit_matches_itself(shop):
    for stated in ("COIL", "coil", "coils", "bundle"):
        resolution = resolve_uom(shop.product(WIRE_90), stated)
        assert resolution["status"] == UOM_MATCHES, stated
        assert resolution["needsClarification"] is False


def test_11_the_base_unit_is_convertible_but_still_needs_confirming(shop):
    """The conversion is KNOWN, which is exactly why it is not applied."""
    resolution = resolve_uom(shop.product(WIRE_90), "metres")

    assert resolution["status"] == UOM_CONVERTIBLE
    assert resolution["needsClarification"] is True
    assert "1 COIL = 90 METER" in resolution["message"]
    assert "will not convert this for you" in resolution["message"]


def test_12_an_unrelated_unit_cannot_be_reconciled(shop):
    resolution = resolve_uom(shop.product(SWITCH), "box")

    assert resolution["status"] == UOM_NOT_RECONCILABLE
    assert resolution["needsClarification"] is True
    assert "not sold by the box" in resolution["message"]


def test_12b_a_unit_this_shop_does_not_know_is_refused_not_ignored(shop):
    resolution = resolve_uom(shop.product(SWITCH), "dozen")
    assert resolution["status"] == UOM_NOT_RECONCILABLE
    assert "not a unit this shop sells in" in resolution["message"]


def test_12c_metres_of_conduit_are_refused_because_nothing_is_configured(shop):
    """Conduit is measured in metres in the world. It is not measured in
    metres in this catalogue, and the catalogue is what decides."""
    resolution = resolve_uom(shop.product(CONDUIT), "metres")
    assert resolution["status"] == UOM_NOT_RECONCILABLE


# ---------------------------------------------------------------------------
# 13-15  the quotation
# ---------------------------------------------------------------------------

def test_13_the_canonical_quotation_is_unchanged(shop):
    """The existing flow states no units and must behave exactly as before."""
    quote = calculate_quote(shop, CANONICAL).as_dict()

    assert quote["total"] == 22306.48
    for line in quote["lines"]:
        assert line["requestedUom"] is None
        assert line["uomStatus"] == UOM_UNSPECIFIED


def test_13b_stating_the_correct_unit_changes_no_figure(shop):
    plain = calculate_quote(shop, CANONICAL).as_dict()
    with_units = calculate_quote(shop, [
        {"skuId": SWITCH, "quantity": 20, "uom": "pieces"},
        {"skuId": WIRE_90, "quantity": 3, "uom": "coils"},
        {"skuId": MCB, "quantity": 2, "uom": "piece"},
    ]).as_dict()

    assert with_units["total"] == plain["total"] == 22306.48
    for a, b in zip(plain["lines"], with_units["lines"]):
        assert a["quantity"] == b["quantity"]
        assert a["lineTotal"] == b["lineTotal"]
        assert a["shortageQty"] == b["shortageQty"]


def test_14_the_quotation_keeps_the_requested_unit_and_shows_the_equivalent(shop):
    quote = calculate_quote(
        shop, [{"skuId": WIRE_90, "quantity": 3, "uom": "coils"}]).as_dict()
    line = quote["lines"][0]

    # The ordered quantity is untouched.
    assert line["quantity"] == 3
    assert line["requestedUom"] == COIL
    assert line["catalogueUom"] == COIL
    assert line["uomStatus"] == UOM_MATCHES
    # The equivalent is beside it, not instead of it.
    assert line["baseEquivalent"]["quantity"] == 270.0
    assert line["baseEquivalent"]["uom"] == METER
    assert line["conversion"] == "1 COIL = 90 METER"
    assert line["evidence"]["units"] == "3 COIL = 270.0 METER"
    # And the price is still per coil.
    assert line["lineTotal"] == 19824.0


def test_15_a_metre_order_stops_the_quotation_rather_than_becoming_coils(shop):
    """The failure this whole module exists to prevent."""
    with pytest.raises(UomMismatchError) as caught:
        calculate_quote(shop, [{"skuId": WIRE_90, "quantity": 90, "uom": "metres"}])

    assert caught.value.skuId == WIRE_90
    assert caught.value.resolution["status"] == UOM_CONVERTIBLE


def test_15b_even_an_exact_multiple_is_not_converted(shop):
    """180 metres IS two 90 m coils. It is still not quietly made into two."""
    with pytest.raises(UomMismatchError):
        calculate_quote(shop, [{"skuId": WIRE_90, "quantity": 180, "uom": "metres"}])


def test_15c_a_box_of_switches_stops_the_quotation(shop):
    with pytest.raises(UomMismatchError) as caught:
        calculate_quote(shop, [{"skuId": SWITCH, "quantity": 2, "uom": "boxes"}])
    assert caught.value.resolution["status"] == UOM_NOT_RECONCILABLE


def test_15d_a_rejected_line_produces_no_quotation_at_all(shop):
    """Not a quote with a warning on it. A quote that was not made."""
    with pytest.raises(UomMismatchError):
        calculate_quote(shop, [
            {"skuId": SWITCH, "quantity": 20},
            {"skuId": WIRE_90, "quantity": 90, "uom": "metres"},
        ])


# ---------------------------------------------------------------------------
# 16  matching
# ---------------------------------------------------------------------------

def test_16_a_box_request_matches_only_a_sku_stocked_in_boxes(shop):
    result = resolve_product(
        shop, requested_text="Anchor modular switch 1-Way 10A", uom="BOX")
    assert result.status == NOT_FOUND


def test_16b_the_same_request_in_pieces_resolves(shop):
    result = resolve_product(
        shop, requested_text="Anchor modular switch 1-Way 10A", uom="PIECE")
    assert result.status == RESOLVED
    assert result.skuId == SWITCH


def test_16c_a_coil_request_resolves_the_coil_sku(shop):
    result = resolve_product(
        shop, requested_text="Finolex 1.5 sqmm red wire 90m", uom="coils")
    assert result.status == RESOLVED
    assert result.skuId == WIRE_90


def test_16d_candidates_report_the_unit_they_are_sold_in(shop):
    result = resolve_product(shop, requested_text="Finolex 1.5 sqmm red wire")
    assert result.candidates
    assert all(c["uom"] in STOCKING_UOMS for c in result.candidates)


# ---------------------------------------------------------------------------
# 17  inventory
# ---------------------------------------------------------------------------

def test_17_inventory_is_counted_in_the_skus_own_unit(shop):
    """One coil in stock is one coil, not ninety metres."""
    rows = {r["skuId"]: r for r in check_inventory(shop, [WIRE_90, SWITCH])}

    wire = rows[WIRE_90]
    assert wire["uom"] == COIL
    assert wire["onHand"] == shop.onHand(WIRE_90) == 1
    # The equivalent is offered alongside, and the count is not restated in it.
    assert wire["baseEquivalent"]["quantity"] == 90.0
    assert rows[SWITCH]["baseEquivalent"] is None


def test_17b_a_shortage_is_expressed_in_catalogue_units(shop):
    """3 coils wanted, 1 in stock, 2 short - never 180 metres short."""
    quote = calculate_quote(
        shop, [{"skuId": WIRE_90, "quantity": 3, "uom": "coils"}]).as_dict()
    line = quote["lines"][0]

    assert line["onHand"] == 1
    assert line["shortageQty"] == 2
    assert line["catalogueUom"] == COIL


def test_17c_adding_units_moved_no_stock_figure(shop):
    """Regression guard around the whole feature."""
    rows = check_inventory(shop, list(shop.products))
    assert all(r["onHand"] == shop.onHand(r["skuId"]) for r in rows)


# ---------------------------------------------------------------------------
# 18  the catalogue itself
# ---------------------------------------------------------------------------

def test_18_a_product_written_before_this_module_still_resolves():
    """`uom` is absent on a legacy Product; the old `unit` string carries it."""
    legacy = make_product("OLD-1", cost=10.0, selling=20.0)
    assert legacy.uom == ""
    assert product_uom(legacy) == PIECE
    assert not has_conversion(legacy)


def test_18b_a_conversion_is_only_used_where_the_product_declares_one():
    with_conv = Product(
        skuId="X-1", brand="B", category="Wire", specification="1.5 sqmm",
        colour="Red", length="45m", unit="coil", sellingPrice=100.0,
        costPrice=50.0, supplierId="S-FAST", name="Test coil",
        uom="COIL", uomQuantity=1.0, baseQuantity=45.0, baseUom="METER",
    )
    assert base_equivalent(with_conv, 2)["quantity"] == 90.0
    assert conversion_note(with_conv) == "1 COIL = 45 METER"


def test_18c_the_seeded_catalogue_declares_a_unit_for_every_sku(shop):
    for sku, product in shop.products.items():
        assert product.uom, sku
        assert normalize_uom(product.uom) == product.uom, sku
        # Where a conversion exists it must be complete and positive.
        if product.baseQuantity is not None:
            assert product.baseQuantity > 0, sku
            assert normalize_uom(product.baseUom) is not None, sku


def test_18d_the_conversion_matches_the_length_printed_on_the_sku(shop):
    """The 90 comes off the catalogue, and this proves it rather than
    restating it."""
    for sku, product in shop.products.items():
        if not has_conversion(product):
            continue
        assert product.length, sku
        assert float(str(product.length).lower().rstrip("m")) == product.baseQuantity


def test_18e_no_uom_field_changed_a_price_or_a_cost(shop):
    """The seed was regenerated to add units. Nothing else may have moved."""
    tiny = make_dataset([make_product("P", cost=10.0, selling=20.0)])
    assert tiny.product("P").sellingPrice == 20.0
    assert shop.product(WIRE_90).sellingPrice == 6608.0
    assert shop.product(WIRE_90).costPrice == 5900.0
    assert shop.product(SWITCH).sellingPrice == 78.3
