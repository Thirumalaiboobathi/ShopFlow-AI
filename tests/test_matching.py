"""Deterministic SKU resolution.

The model never chooses a SKU. These tests pin the rules that do.
"""

from __future__ import annotations

from conftest import make_dataset, make_product
from engine.matching import (
    AMBIGUOUS,
    NOT_FOUND,
    RESOLVED,
    resolve_product,
    search_catalog,
    sku_exists,
)


def test_exact_attributes_resolve_to_one_sku(seeded):
    r = resolve_product(seeded, requested_text="Polycab 2.5 blue wire",
                        brand="Polycab", category="Wire",
                        specification="2.5 sqmm", colour="Blue")
    assert r.status == RESOLVED
    assert r.skuId == "W-POL-2.5-BLU-90M"
    assert r.resolvedBy == "exact"


def test_missing_colour_is_ambiguous_and_never_guessed(seeded):
    """Colour and coil length both vary here, so there is no single clean
    question - the owner is offered the actual products instead."""
    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm wire",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm")
    assert r.status == AMBIGUOUS
    assert r.skuId is None
    assert {o["skuId"] for o in r.options} == {
        "W-FIN-1.5-RED-90M", "W-FIN-1.5-BLU-90M",
        "W-FIN-1.5-BLK-90M", "W-FIN-1.5-RED-180M",
    }


def test_missing_colour_asks_by_colour_when_length_is_pinned(seeded):
    """With the coil length given, colour is the only thing left to ask."""
    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm wire 90m",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm", length="90m")
    assert r.status == AMBIGUOUS
    assert r.clarifyingAttribute == "colour"
    assert {o["value"] for o in r.options} == {"Red", "Blue", "Black"}


def test_a_shop_default_never_silently_wins_a_tie(seeded):
    """Red 1.5 exists in 90m and 180m. The 90m coil is the shop's default and
    must still not be chosen on the customer's behalf - a coil of the wrong
    length is the wrong price and the wrong delivery."""
    default = seeded.product("W-FIN-1.5-RED-90M")
    assert default.isDefaultVariant is True

    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm red wire",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm", colour="Red")
    assert r.status == AMBIGUOUS
    assert r.skuId is None
    assert r.clarifyingAttribute == "length"
    assert {o["value"] for o in r.options} == {"90m", "180m"}


def test_generic_switch_request_asks_which_variant(seeded):
    r = resolve_product(seeded, requested_text="20 Anchor modular switches",
                        brand="Anchor", category="Switch")
    assert r.status == AMBIGUOUS
    assert r.skuId is None
    assert r.clarifyingAttribute == "specification"
    assert "1-Way 10A" in {o["value"] for o in r.options}


def test_explicit_switch_variant_resolves_exactly(seeded):
    r = resolve_product(seeded,
                        requested_text="20 Anchor modular switches 1-Way 10A",
                        brand="Anchor", category="Switch",
                        specification="1-Way 10A")
    assert r.status == RESOLVED
    assert r.skuId == "SW-ANC-1W10A"
    assert r.resolvedBy == "exact"


def test_generic_mcb_request_asks_rather_than_picking_a_brand(seeded):
    """Six 32A MCBs exist across three brands - picking one would be a
    silent brand substitution, even though the customer named no brand."""
    r = resolve_product(seeded, requested_text="2 MCB 32 amp",
                        category="MCB", specification="32A")
    assert r.status == AMBIGUOUS
    assert r.skuId is None
    assert len(r.options) == 6


def test_explicit_mcb_resolves_exactly(seeded):
    r = resolve_product(seeded, requested_text="2 Havells MCB SP 32A",
                        category="MCB", brand="Havells",
                        specification="SP 32A")
    assert r.status == RESOLVED
    assert r.skuId == "MCB-HAV-SP-32A-C"
    assert r.resolvedBy == "exact"


def test_explicit_wire_with_length_resolves_exactly(seeded):
    r = resolve_product(seeded,
                        requested_text="Finolex 1.5 sq mm red wire 90m",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm", colour="Red", length="90m")
    assert r.status == RESOLVED
    assert r.skuId == "W-FIN-1.5-RED-90M"
    assert r.resolvedBy == "exact"


def test_default_variant_metadata_is_retained_on_the_catalog(seeded):
    """The flag stays for owner-facing features later; it just has no vote
    in customer order processing."""
    defaults = [p for p in seeded.products.values() if p.isDefaultVariant]
    assert len(defaults) > 0


def test_named_brand_is_never_replaced_by_another(seeded):
    """Asking for Finolex must never return Polycab, whatever else matches."""
    candidates = search_catalog(seeded, query="1.5 sq mm wire", brand="Finolex")
    assert candidates
    assert all(c.product.brand == "Finolex" for c in candidates)


def test_unknown_brand_returns_not_found_rather_than_a_substitute(seeded):
    r = resolve_product(seeded, requested_text="Acme 1.5 wire",
                        brand="NoSuchBrand", category="Wire")
    assert r.status == NOT_FOUND
    assert r.skuId is None


def test_every_resolved_sku_exists_in_the_catalog(seeded):
    for text, attrs in [
        ("Anchor modular switches", {"brand": "Anchor", "category": "Switch"}),
        ("MCB 32 amp", {"category": "MCB", "specification": "32A"}),
        ("Philips 9W bulb", {"brand": "Philips", "category": "LED Lamp"}),
    ]:
        r = resolve_product(seeded, requested_text=text, **attrs)
        if r.status == RESOLVED:
            assert sku_exists(seeded, r.skuId)


def test_an_ambiguous_result_never_carries_a_sku(seeded):
    """Nothing downstream can accidentally treat a question as an answer."""
    for text, attrs in [
        ("Anchor modular switches", {"brand": "Anchor", "category": "Switch"}),
        ("MCB 32 amp", {"category": "MCB", "specification": "32A"}),
        ("Finolex 1.5 sq mm wire", {"brand": "Finolex", "category": "Wire",
                                    "specification": "1.5 sqmm"}),
    ]:
        r = resolve_product(seeded, requested_text=text, **attrs)
        assert r.status == AMBIGUOUS
        assert r.skuId is None
        assert r.resolvedBy is None
        assert len(r.options) > 1


def test_ambiguity_options_are_all_real_skus(seeded):
    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm wire",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm")
    for option in r.options:
        assert sku_exists(seeded, option["skuId"])


def test_single_candidate_needs_no_default_flag():
    products = [make_product("ONLY", 100, 150, brand="B1", category="Wire",
                             specification="1.5 sqmm", colour="Red")]
    data = make_dataset(products)
    r = resolve_product(data, requested_text="B1 red wire", brand="B1")
    assert r.status == RESOLVED and r.skuId == "ONLY"


def test_no_default_among_equals_forces_a_question():
    products = [
        make_product("A", 100, 150, brand="B1", category="Wire",
                     specification="1.5 sqmm", colour="Red"),
        make_product("B", 100, 150, brand="B1", category="Wire",
                     specification="1.5 sqmm", colour="Blue"),
    ]
    data = make_dataset(products)
    r = resolve_product(data, requested_text="B1 1.5 wire", brand="B1")
    assert r.status == AMBIGUOUS
    assert r.clarifyingAttribute == "colour"
