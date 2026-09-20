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
    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm wire",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm")
    assert r.status == AMBIGUOUS
    assert r.skuId is None
    assert r.clarifyingAttribute == "colour"
    assert {o["value"] for o in r.options} == {"Red", "Blue", "Black"}


def test_shop_default_breaks_a_length_tie_and_reports_it(seeded):
    """Red 1.5 exists in 90m and 180m; the standard coil wins, visibly."""
    r = resolve_product(seeded, requested_text="Finolex 1.5 sq mm red wire",
                        brand="Finolex", category="Wire",
                        specification="1.5 sqmm", colour="Red")
    assert r.status == RESOLVED
    assert r.skuId == "W-FIN-1.5-RED-90M"
    assert r.resolvedBy == "shop-default"
    assert [a["skuId"] for a in r.alternatives] == ["W-FIN-1.5-RED-180M"]


def test_canonical_switch_request_resolves_to_the_default_variant(seeded):
    r = resolve_product(seeded, requested_text="20 Anchor modular switches",
                        brand="Anchor", category="Switch")
    assert r.status == RESOLVED
    assert r.skuId == "SW-ANC-1W10A"
    assert r.resolvedBy == "shop-default"
    assert len(r.alternatives) == 5


def test_canonical_mcb_request_resolves(seeded):
    r = resolve_product(seeded, requested_text="2 MCB 32 amp",
                        category="MCB", specification="32A")
    assert r.status == RESOLVED
    assert r.skuId == "MCB-HAV-SP-32A-C"


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
