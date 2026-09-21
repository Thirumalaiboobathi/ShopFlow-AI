"""Voice as an interface over the existing workflows.

The point of these tests is not that speech recognition works - that happens in
the browser and is not ours to test. It is that a transcript reaching the
backend is subjected to exactly the same rules as typed text: the same matcher,
the same ambiguity handling, the same refusal to invent, and the same human
confirmation boundary.

The last block is the important one. Every business number a voice reply
speaks must be traceable to a value the deterministic engine produced.
"""

from __future__ import annotations

import re

import pytest

from engine.loader import cached_dataset
from engine.margin import margin_view
from engine.uom import split_quantity_and_uom
from engine.matching import AMBIGUOUS, NOT_FOUND, RESOLVED
from engine.voice import (
    ENGLISH,
    INTENT_AVAILABILITY,
    INTENT_CONFIRM_PRICE,
    INTENT_MARGIN,
    INTENT_ORDER,
    INTENT_PRICE,
    INTENT_PURCHASE_PLAN,
    INTENT_QUOTE_TOTAL,
    INTENT_STOCK,
    MIXED,
    TAMIL,
    answer_shop_query,
    classify_intent,
    detect_language,
    extract_attributes,
    extract_budget,
    normalize_transcript,
    speak_margin,
    speak_plan,
    speak_quote,
    speaks_tamil,
)

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"


@pytest.fixture(scope="module")
def shop():
    return cached_dataset()


def numbers_in(text):
    """Every numeric token in a string, for fabrication checks."""
    return {t.replace(",", "") for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


# ---------------------------------------------------------------------------
# 1-3  English, Tamil and Tanglish transcripts reach the order flow
# ---------------------------------------------------------------------------

def test_1_english_transcript_is_routed_to_the_order_workflow(shop):
    result = answer_shop_query(shop, "Give me 20 Anchor modular switches 1-Way 10A")
    assert result["intent"] == INTENT_ORDER
    assert result["status"] == "DELEGATE"
    assert result["delegateTo"] == INTENT_ORDER
    # Voice must not answer an order itself.
    assert result["handledBy"] == "existing-workflow"
    assert result["language"] == ENGLISH


def test_2_tamil_script_transcript_is_detected_and_routed(shop):
    result = answer_shop_query(shop, "20 Anchor modular switch வேண்டும்")
    assert result["language"] in (TAMIL, MIXED)
    assert speaks_tamil(result["language"])
    assert result["delegateTo"] == INTENT_ORDER


def test_3_tanglish_transcript_is_detected_and_routed(shop):
    result = answer_shop_query(
        shop, "Anna, 20 Anchor modular switch 10 amp venum")
    assert result["language"] == MIXED
    assert result["delegateTo"] == INTENT_ORDER


def test_3b_english_input_never_forces_a_tamil_reply(shop):
    result = answer_shop_query(shop, "Anchor switch 1-Way 10A stock?")
    assert result["language"] == ENGLISH
    assert not speaks_tamil(result["language"])
    # An English question gets an English answer.
    assert "irukku" not in result["spokenText"]


# ---------------------------------------------------------------------------
# 4-5  ambiguity and rejection, unchanged from the typed flow
# ---------------------------------------------------------------------------

def test_4_an_ambiguous_product_asks_instead_of_choosing(shop):
    result = answer_shop_query(shop, "Anchor switch stock la evlo irukku?")

    assert result["status"] == AMBIGUOUS
    assert result["clarifyingAttribute"]
    assert len(result["options"]) > 1
    assert result["spokenText"]
    # No product was picked.
    assert "product" not in result


def test_4b_the_spoken_clarification_names_no_single_product(shop):
    result = answer_shop_query(shop, "Finolex wire price enna?")
    assert result["status"] == AMBIGUOUS
    # The reply asks a question; it does not announce a price.
    assert "₹" not in result["spokenText"]


def test_5_an_unknown_product_is_refused(shop):
    result = answer_shop_query(shop, "Godrej almirah stock?")
    assert result["status"] == NOT_FOUND
    assert "product" not in result
    assert "₹" not in result["spokenText"]


def test_5b_gibberish_is_not_understood_rather_than_guessed(shop):
    result = answer_shop_query(shop, "asdfgh qwerty zxcvbn")
    assert result["status"] in ("NOT_UNDERSTOOD", NOT_FOUND)
    assert "₹" not in result["spokenText"]


# ---------------------------------------------------------------------------
# 6-9  lookups use the real engine values
# ---------------------------------------------------------------------------

def test_6_stock_query_returns_the_actual_inventory(shop):
    result = answer_shop_query(shop, "Anchor switch 1-Way 10A stock la evlo?")

    assert result["status"] == RESOLVED
    sku = result["product"]["skuId"]
    assert result["product"]["onHand"] == shop.onHand(sku)
    # The spoken figure is the inventory figure, not a paraphrase.
    assert str(shop.onHand(sku)) in result["spokenText"]


def test_7_price_query_returns_the_actual_selling_price(shop):
    result = answer_shop_query(shop, "Finolex 1.5 sq mm red wire 90m price enna?")

    assert result["status"] == RESOLVED
    assert result["product"]["skuId"] == WIRE
    assert result["product"]["sellingPrice"] == shop.product(WIRE).sellingPrice
    assert "6,608.00" in result["spokenText"]


def test_7b_price_query_speaks_selling_price_not_supplier_cost(shop):
    """The customer-facing number, never what the shop pays."""
    from engine.pricing import current_cost

    result = answer_shop_query(shop, "Finolex 1.5 sq mm red wire 90m price enna?")
    spoken = numbers_in(result["spokenText"])

    assert str(shop.product(WIRE).sellingPrice) in {n for n in spoken} or \
        "6608.00" in spoken
    assert str(current_cost(shop, WIRE)) not in spoken


def test_8_availability_query_uses_real_stock(shop):
    result = answer_shop_query(shop, "Havells MCB SP 32 amp irukka?")

    assert result["status"] == RESOLVED
    assert result["product"]["skuId"] == MCB
    on_hand = shop.onHand(MCB)
    assert result["product"]["onHand"] == on_hand
    assert str(on_hand) in result["spokenText"]


def test_8b_quote_total_is_read_from_the_engine_quote(shop):
    """speak_quote formats a quote; it never computes one."""
    quote = {"total": 22306.48, "lines": [
        {"name": "Anchor Modular Switch", "shortageQty": 6, "unit": "piece"},
        {"name": "Finolex Wire", "shortageQty": 2, "unit": "coil"},
    ]}
    spoken = speak_quote(quote, tamil=False)

    assert "22,306.48" in spoken
    assert "6" in spoken and "2" in spoken
    # Nothing beyond the engine's own figures appears.
    assert numbers_in(spoken) <= {"22306.48", "6", "2"}


def test_9_purchase_plan_query_is_delegated_with_the_spoken_budget(shop):
    result = answer_shop_query(shop, "5000 budget la enna purchase panna mudiyum?")

    assert result["intent"] == INTENT_PURCHASE_PLAN
    assert result["status"] == "DELEGATE"
    assert result["budget"] == 5000.0
    # Voice does not plan; the deterministic planner does.
    assert result["handledBy"] == "existing-workflow"


def test_9b_speak_plan_only_repeats_planner_figures(shop):
    from engine.purchasing import build_purchase_plan

    plan = build_purchase_plan(shop, 5000.0)
    spoken = speak_plan(plan, tamil=False)

    assert numbers_in(spoken) <= {
        "5,000.00".replace(",", ""),
        str(plan["counts"]["restockSelected"]),
        str(plan["counts"]["restockDeferred"]),
        f"{plan['remaining']:.2f}",
        "5000.00",
    }


def test_9c_quote_total_question_is_delegated_not_answered(shop):
    result = answer_shop_query(shop, "Indha order total evlo?")
    assert result["intent"] == INTENT_QUOTE_TOTAL
    assert result["status"] == "DELEGATE"
    assert result["spokenText"] == ""


# ---------------------------------------------------------------------------
# 12  a spoken instruction cannot bypass human confirmation
# ---------------------------------------------------------------------------

def test_12_voice_cannot_confirm_a_supplier_price(shop):
    for phrase in ("Confirm the supplier price",
                   "Supplier price confirm pannunga",
                   "Approve the new supplier cost"):
        result = answer_shop_query(shop, phrase)
        assert result["intent"] == INTENT_CONFIRM_PRICE
        assert result["status"] == "NEEDS_HUMAN_CONFIRMATION"
        assert result["requiresConfirmation"] is True
        # Nothing was decided, and no price is quoted back as agreed.
        assert "confirmed" not in result["spokenText"].lower()


def test_12b_the_voice_module_cannot_write_anything(shop):
    """It has no path to persistence - it only reads the dataset."""
    import inspect

    from engine import voice

    source = inspect.getsource(voice)
    for forbidden in ("boto3", "put_item", "update_item", "delete_item",
                      "build_cost_record", "requests"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# 13-16  nothing is fabricated
# ---------------------------------------------------------------------------

def test_13_no_fabricated_sku(shop):
    """Every SKU a voice answer names exists in the catalogue."""
    for phrase in ("Anchor switch 1-Way 10A stock?",
                   "Finolex 1.5 sq mm red wire 90m price enna?",
                   "Havells MCB SP 32 amp irukka?",
                   "Philips LED 5 w cool white stock?"):
        result = answer_shop_query(shop, phrase)
        if result["status"] == RESOLVED:
            assert result["product"]["skuId"] in shop.products
        for option in result.get("options", []):
            assert option["skuId"] in shop.products


def test_14_no_fabricated_price(shop):
    result = answer_shop_query(shop, "Finolex 1.5 sq mm red wire 90m price enna?")
    real = shop.product(result["product"]["skuId"]).sellingPrice

    spoken = numbers_in(result["spokenText"])
    # Only the real price (and no other rupee figure) may be spoken.
    assert f"{real:.2f}" in spoken or str(real) in spoken
    for value in spoken:
        assert value in {f"{real:.2f}", str(real), str(int(real)),
                         "1.5", "90", "5"}


def test_15_no_fabricated_inventory(shop):
    for phrase in ("Anchor switch 1-Way 10A stock?",
                   "Havells MCB SP 32 amp irukka?"):
        result = answer_shop_query(shop, phrase)
        assert result["status"] == RESOLVED
        sku = result["product"]["skuId"]
        assert result["product"]["onHand"] == shop.onHand(sku)


def test_16_no_fabricated_total(shop):
    """speak_quote is a formatter. Given no lines it invents no shortages."""
    spoken = speak_quote({"total": 100.0, "lines": []}, tamil=False)
    assert numbers_in(spoken) == {"100.00"}
    assert "short" not in spoken.lower()


def test_16b_voice_never_recomputes_a_business_number(shop):
    """Business values are read and formatted, never combined.

    An earlier version of this test banned `sum(` outright, which was wrong -
    detect_language legitimately counts words. What matters is that no
    catalogue or inventory value is used in arithmetic, so the check is
    against those names specifically.
    """
    import inspect

    from engine import voice

    source = inspect.getsource(voice)
    for name in ("sellingPrice", "onHand", "costPrice", "unitCost"):
        for operator in ("*", "+", "-", "/"):
            assert f"{name} {operator}" not in source, f"{name} {operator}"
            assert f"{operator} {name}" not in source, f"{operator} {name}"

    # And the only figures a resolved answer can speak are the ones it read.
    result = answer_shop_query(shop, "Havells MCB SP 32 amp irukka?")
    product = result["product"]
    allowed = {str(product["onHand"]), f"{product['sellingPrice']:.2f}",
               "32", "32.00"}
    assert numbers_in(result["spokenText"]) <= allowed


# ---------------------------------------------------------------------------
# normalisation, language, attributes
# ---------------------------------------------------------------------------

def test_normalisation_reports_every_substitution():
    cleaned, applied = normalize_transcript("3 coils Finolex 1.5 square mm wire")
    assert "coil" in cleaned and "sq mm" in cleaned
    assert applied  # the owner can see what was changed
    assert any("coils" in a for a in applied)


def test_normalisation_preserves_decimal_gauges():
    """A full stop between digits is a decimal point, not punctuation.

    Stripping it turned "1.5 sq mm" into "1 5" and lost the wire gauge.
    """
    cleaned, _ = normalize_transcript("Finolex 1.5 sq mm red wire.")
    assert "1.5" in cleaned
    assert not cleaned.endswith(".")


def test_normalisation_never_maps_a_phrase_to_a_sku():
    cleaned, _ = normalize_transcript("Anchor switch venum")
    assert "SW-" not in cleaned
    assert "-" not in cleaned.replace("1-Way", "")


@pytest.mark.parametrize("text,expected", [
    ("Give me 20 Anchor switches", ENGLISH),
    ("20 Anchor modular switch வேண்டும்", MIXED),
    ("Anna 20 Anchor switch venum", MIXED),
    ("Anchor switch stock la evlo irukku", MIXED),
])
def test_language_detection(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize("text,intent", [
    ("Anchor switch stock la evlo?", INTENT_STOCK),
    ("Finolex red wire price enna?", INTENT_PRICE),
    ("2 Havells MCB 32 amp irukka?", INTENT_AVAILABILITY),
    ("Indha order total evlo?", INTENT_QUOTE_TOTAL),
    ("5000 budget la enna purchase panna mudiyum?", INTENT_PURCHASE_PLAN),
    ("Anna 20 Anchor switch venum", INTENT_ORDER),
])
def test_intent_classification(text, intent):
    assert classify_intent(text)["intent"] == intent


def test_budget_extraction_reads_the_spoken_number():
    assert extract_budget("5000 budget la enna vaanga mudiyum") == 5000.0
    assert extract_budget("25,000 budget") == 25000.0
    assert extract_budget("no number here") is None


def test_extracted_attributes_only_use_real_catalogue_values(shop):
    attrs = extract_attributes(shop, "finolex 1.5 sq mm red wire 90m")

    brands = {p.brand for p in shop.products.values()}
    colours = {p.colour for p in shop.products.values() if p.colour}
    lengths = {p.length for p in shop.products.values() if p.length}
    categories = {p.category for p in shop.products.values()}

    assert attrs["brand"] in brands
    assert attrs["colour"] in colours
    assert attrs["length"] in lengths
    assert attrs["category"] in categories


def test_a_bare_quantity_is_not_read_as_a_specification(shop):
    """"2 Havells MCB" - the 2 is how many, not which."""
    attrs = extract_attributes(shop, "2 havells mcb sp 32 amp")
    # Token comparison, not substring - "32" legitimately contains a "2".
    tokens = (attrs.get("specification") or "").split()
    assert "2" not in tokens
    assert "32" in tokens


# ---------------------------------------------------------------------------
# 15-16  margin questions - answered by the engine, only spoken here
# ---------------------------------------------------------------------------

WIRE = "W-FIN-1.5-RED-90M"
CONFIRMED_WIRE_COST = 6300.0


def test_15_a_margin_question_is_recognised_in_english_and_tanglish():
    for said in (
        "What is the margin on Finolex 1.5 sq mm red wire 90m?",
        "Indha Finolex 1.5 sq mm red wire 90m la margin evlo?",
        "Finolex 1.5 sq mm red wire 90m profit enna?",
    ):
        normalized, _ = normalize_transcript(said)
        assert classify_intent(normalized)["intent"] == INTENT_MARGIN, said


def test_15b_margin_beats_price_when_both_words_are_present():
    """"Margin" and "price" in one sentence is a margin question."""
    assert classify_intent("what is the margin at this price")["intent"] == INTENT_MARGIN


def test_15c_a_margin_answer_carries_the_engines_own_view():
    data = cached_dataset()
    result = answer_shop_query(
        data, "What is the margin on Finolex 1.5 sq mm red wire 90m?",
        {WIRE: CONFIRMED_WIRE_COST})

    assert result["status"] == RESOLVED
    assert result["intent"] == INTENT_MARGIN
    # The whole margin view is passed through untouched, so the UI and the
    # spoken line are reading the same object.
    assert result["margin"] == margin_view(data, WIRE, CONFIRMED_WIRE_COST)


def test_15d_the_spoken_margin_uses_the_engines_figures(seeded_margin):
    spoken = speak_margin(seeded_margin, tamil=False)
    for field in ("newMarginAmount", "oldMarginAmount", "marginReductionAmount"):
        assert _money_words(seeded_margin[field])[0] in spoken, field
    assert "Current margin" in spoken and "Previous margin" in spoken


def test_15e_with_no_confirmed_cost_it_says_so_rather_than_comparing():
    data = cached_dataset()
    result = answer_shop_query(
        data, "What is the margin on Finolex 1.5 sq mm red wire 90m?")

    assert result["margin"]["comparisonAvailable"] is False
    assert "No supplier price change has been confirmed" in result["spokenText"]
    # Nothing that would need a second figure is spoken.
    assert "Previous margin" not in result["spokenText"]


def test_15f_an_ambiguous_margin_question_still_asks_rather_than_guessing():
    result = answer_shop_query(
        cached_dataset(), "Anchor switch margin evlo?", {WIRE: CONFIRMED_WIRE_COST})
    assert result["status"] == AMBIGUOUS
    assert "margin" not in result, "no margin may be reported without a resolved SKU"


def test_16_speak_margin_performs_no_arithmetic_of_its_own():
    """Hand it figures that cannot be derived from each other and check they
    come back verbatim. A function that computed anything would disagree."""
    invented = {
        "available": True,
        "comparisonAvailable": True,
        "productName": "Test Product",
        "unit": "piece",
        "newMarginAmount": 1.0,
        "oldMarginAmount": 2.0,
        # Deliberately NOT 2.0 - 1.0. If this appears as 1.00, the function
        # subtracted rather than read.
        "marginReductionAmount": 77.0,
    }
    spoken = speak_margin(invented, tamil=False)
    assert "77.00" in spoken
    assert "Current margin \u20b91.00" in spoken
    assert "Previous margin \u20b92.00" in spoken


def test_16b_the_voice_layer_contains_no_margin_formula():
    import inspect

    from engine import voice as voice_module

    source = inspect.getsource(voice_module)
    for formula in ("sellingPrice -", "- previousSupplierCost",
                    "- confirmedSupplierCost", "* 100", "/ 100"):
        assert formula not in source, (
            f"voice.py appears to compute a margin itself ({formula!r})")


def test_16c_voice_never_reports_a_margin_it_was_not_given():
    """No confirmed cost in, no confirmed-cost figure out."""
    result = answer_shop_query(
        cached_dataset(),
        "What is the margin on Finolex 1.5 sq mm red wire 90m?", {})
    assert result["margin"]["confirmedSupplierCost"] is None
    assert "6,300" not in result["spokenText"]


@pytest.fixture()
def seeded_margin():
    return margin_view(cached_dataset(), WIRE, CONFIRMED_WIRE_COST)


def _money_words(value):
    """The formatted forms a rupee figure may legitimately take in speech."""
    return [f"{value:,.2f}"]


# ---------------------------------------------------------------------------
# 17  units survive the voice layer, and the voice layer decides nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said,unit", [
    ("3 coils Finolex 1.5 sq mm red wire", "COIL"),
    ("2 boxes Anchor switch", "BOX"),
    ("90 metres Finolex red wire", "METER"),
    ("90 meters Finolex red wire", "METER"),
    ("20 pieces Anchor switch", "PIECE"),
    ("5 packs cable clip", "PACK"),
    ("4 lengths PVC conduit", "LENGTH"),
])
def test_17_a_spoken_unit_survives_normalisation(said, unit):
    """Voice tidies wording. The unit must still be readable afterwards, by
    the SAME parser the typed flow uses - there is no second implementation."""
    normalized, _ = normalize_transcript(said)
    found = split_quantity_and_uom(normalized)
    assert found is not None, normalized
    assert found["uom"] == unit, normalized


def test_17aa_a_tamil_numeral_word_is_not_read_as_a_quantity():
    """"moonu coils" is three coils to a person and to the language model.
    It is NOT a quantity to the deterministic parser, which reads digits -
    and that is the correct division. The model turns the word into a number
    and the engine then validates the unit; the parser never guesses at a
    numeral it cannot read.
    """
    normalized, _ = normalize_transcript("moonu coils Finolex wire venum")
    assert "coil" in normalized
    assert split_quantity_and_uom(normalized) is None


def test_17b_voice_does_not_convert_a_unit():
    """"90 metres" stays 90 metres all the way through the voice layer."""
    normalized, _ = normalize_transcript("90 metres Finolex red wire")
    found = split_quantity_and_uom(normalized)
    assert found["quantity"] == 90.0
    assert found["uom"] == "METER"
    assert "coil" not in normalized.lower()


def test_17c_the_voice_module_contains_no_unit_conversion_of_its_own():
    import inspect

    from engine import voice as voice_module

    source = inspect.getsource(voice_module)
    for formula in ("baseQuantity", "baseUom", "* 90", "/ 90", "uomQuantity"):
        assert formula not in source, (
            f"voice.py appears to convert units itself ({formula!r})")


def test_17d_a_spoken_order_is_still_delegated_not_answered():
    """Units change nothing about the boundary: an order goes to the existing
    order workflow, whatever unit it was counted in."""
    result = answer_shop_query(
        cached_dataset(), "Anna 3 coils Finolex 1.5 sq mm red wire venum")
    assert result["status"] == "DELEGATE"
    assert result["delegateTo"] == INTENT_ORDER
    assert result["handledBy"] == "existing-workflow"
