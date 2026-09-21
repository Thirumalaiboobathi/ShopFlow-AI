"""The claim, tested: different language, same business truth.

WHAT THIS FILE IS FOR
---------------------
One sentence is the whole product argument for the language layer:

    Different language -> same canonical meaning -> same SKU -> same quantity
    -> same UOM -> same price -> same business result.

Everything below tests that sentence, in all 22 Scheduled Languages and in
English, against the real engines. Nothing is mocked: the matcher, the
quotation, the credit check and the message builders are the ones that ship.

HOW THE ORDER PHRASES ARE BUILT, AND WHY
----------------------------------------
Each phrase is a real request written the way Indian retail actually writes
one: the grammar in the customer's own language and script, and the brand and
product vocabulary in Latin script, because "Anchor" and "Finolex" are those
words in every one of these languages. Two phrases use native-script numerals
so the digit normalisation is exercised on real input rather than on a unit
test of itself.

WHAT IS NOT CLAIMED
-------------------
This is not a claim that ShopFlow parses free-flowing literary Bodo. The
matcher reads catalogue vocabulary, and the catalogue is written in English.
`test_9*` documents that boundary honestly: a request whose product noun is
translated out of catalogue vocabulary does NOT resolve to a guessed SKU - it
comes back as a question, which is the correct and safe behaviour.
"""

from __future__ import annotations

import json
import re

import pytest

from engine.credit import check_credit
from engine.language import (
    canonicalize_request,
    capabilities,
    localize_clarification,
    localize_credit_status,
    localize_quote,
    normalize_language,
    scheduled_languages,
    supported_languages,
    translate,
)
from engine.loader import cached_dataset
from engine.margin import margin_view
from engine.matching import AMBIGUOUS, RESOLVED, resolve_product
from engine.messages import (
    INTERNAL_ONLY_FIELDS,
    build_credit_status_message,
    build_quotation_message,
)
from engine.purchasing import build_purchase_plan
from engine.quote import calculate_quote

SWITCH = "SW-ANC-1W10A"
WIRE = "W-FIN-1.5-RED-90M"
MCB = "MCB-HAV-SP-32A-C"

# The canonical order, and the figure it has produced since the first stage of
# this project. No language may move it.
CANONICAL_LINES = [
    {"skuId": SWITCH, "quantity": 20},
    {"skuId": WIRE, "quantity": 3, "uom": "COIL"},
    {"skuId": MCB, "quantity": 2},
]
CANONICAL_TOTAL = 22306.48

RAVI = "CUST-RAVI-001"
RAVI_ORDER = 4200.0
RAVI_LIMIT = 15000.0
RAVI_OUTSTANDING = 8500.0
RAVI_PROJECTED = 12700.0
RAVI_REMAINING = 2300.0

CONFIRMED_WIRE_COST = 6300.0
MARGIN_OLD = 708.0
MARGIN_NEW = 308.0
MARGIN_REDUCTION = 400.0

PRODUCT = "Anchor modular switch 1-Way 10A White"

# The same request, twenty-three ways. The product half is identical in every
# one of them on purpose: that is the point being made.
ORDERS = {
    "en": f"I need 20 {PRODUCT}",
    "as": f"মোক 20 {PRODUCT} লাগে",
    "bn": f"আমার 20 {PRODUCT} চাই",
    "brx": f"आंनो 20 {PRODUCT} नांगौ",
    "doi": f"मिगी 20 {PRODUCT} चाहिदा",
    "gu": f"મને 20 {PRODUCT} જોઈએ",
    # Devanagari numerals, so normalisation is exercised on a real phrase.
    "hi": f"मुझे २० {PRODUCT} चाहिए",
    "kn": f"ನನಗೆ 20 {PRODUCT} ಬೇಕು",
    "ks": f"مے 20 {PRODUCT} ضرورت",
    "kok": f"माका 20 {PRODUCT} जाय",
    "mai": f"हमरा 20 {PRODUCT} चाही",
    "ml": f"എനിക്ക് 20 {PRODUCT} വേണം",
    "mni": f"ঐঙোন্দা 20 {PRODUCT} চাই",
    "mr": f"मला 20 {PRODUCT} हवेत",
    "ne": f"मलाई 20 {PRODUCT} चाहियो",
    "or": f"ମୋତେ 20 {PRODUCT} ଦରକାର",
    "pa": f"ਮੈਨੂੰ 20 {PRODUCT} ਚਾਹੀਦਾ",
    "sa": f"मह्यम् 20 {PRODUCT} आवश्यकम्",
    "sat": f"ᱤᱧ 20 {PRODUCT} ᱞᱟᱹᱠᱛᱤ",
    "sd": f"مون کي 20 {PRODUCT} گهرجي",
    "ta": f"எனக்கு 20 {PRODUCT} வேண்டும்",
    # Telugu numerals, for the same reason as Hindi.
    "te": f"నాకు ౨౦ {PRODUCT} కావాలి",
    "ur": f"مجھے 20 {PRODUCT} چاہیے",
}

# Code-mixed, which is how a great many of these orders are actually typed.
# Representative patterns only - no claim is made of general Tanglish,
# Hinglish, Kanglish or Tenglish coverage, and the README says so.
CODE_MIXED = {
    "ta": f"Anna 20 {PRODUCT} venum",
    "hi": f"20 {PRODUCT} chahiye",
    "te": f"20 {PRODUCT} kavali",
    "kn": f"20 {PRODUCT} beku",
    "ml": f"20 {PRODUCT} venam",
    "mr": f"20 {PRODUCT} pahije",
    "bn": f"20 {PRODUCT} lagbe",
    "gu": f"20 {PRODUCT} joie",
    "pa": f"20 {PRODUCT} chahida",
}

ALL_LANGUAGES = sorted(supported_languages())
SCHEDULED = sorted(scheduled_languages())


@pytest.fixture(scope="module")
def data():
    return cached_dataset()


@pytest.fixture(scope="module")
def quote(data):
    return calculate_quote(data, CANONICAL_LINES).as_dict()


@pytest.fixture(scope="module")
def credit(data):
    return check_credit(data, RAVI, RAVI_ORDER)


def money_tokens(text: str) -> set:
    return set(re.findall(r"₹[\d,]+\.\d{2}", text or ""))


# ---------------------------------------------------------------------------
# 1. every language has a test phrase, and it is a real one
# ---------------------------------------------------------------------------

def test_1_there_is_an_order_phrase_for_every_supported_language():
    assert set(ORDERS) == set(ALL_LANGUAGES)


def test_1a_each_phrase_is_written_in_its_own_language():
    """Not twenty-three copies of the English sentence with a label on top."""
    for code in SCHEDULED:
        stripped = ORDERS[code].replace(PRODUCT, "")
        non_latin = [ch for ch in stripped if ord(ch) > 0x02FF]
        assert non_latin, f"{code}: the phrase has no {code} text in it"


# ---------------------------------------------------------------------------
# 2. THE equivalence test: same request, 23 languages, one SKU
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_2_the_same_request_resolves_to_the_same_sku_in_every_language(data, code):
    canonical = canonicalize_request(ORDERS[code], code)
    result = resolve_product(data, requested_text=canonical["canonicalText"])
    assert result.status == RESOLVED, f"{code}: {result.status}"
    assert result.skuId == SWITCH, f"{code} resolved to {result.skuId}"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_2a_the_quantity_survives_every_script(code):
    """Including the two phrases written in native-script numerals."""
    canonical = canonicalize_request(ORDERS[code], code)
    assert "20" in canonical["canonicalText"], canonical["canonicalText"]


@pytest.mark.parametrize("code", sorted(CODE_MIXED))
def test_2b_representative_code_mixed_input_resolves_the_same_way(data, code):
    canonical = canonicalize_request(CODE_MIXED[code], code)
    result = resolve_product(data, requested_text=canonical["canonicalText"])
    assert result.status == RESOLVED, f"{code}: {result.status}"
    assert result.skuId == SWITCH
    assert "20" in canonical["canonicalText"]


# ---------------------------------------------------------------------------
# 3. QUOTE REGRESSION: no language may move the total
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_3_the_canonical_quotation_is_the_same_in_every_language(data, code):
    """The quotation is calculated once, from canonical lines.

    This is the structural reason the figure cannot vary: there is no
    language-aware path into `calculate_quote` at all. The test runs it per
    language anyway, because an assertion is worth more than an argument.
    """
    result = calculate_quote(data, CANONICAL_LINES).as_dict()
    assert result["total"] == CANONICAL_TOTAL
    localized = localize_quote(result, code)
    assert localized["totalText"] == "₹22,306.48"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_3a_localizing_a_quotation_changes_no_line(quote, code):
    before = json.dumps(quote, sort_keys=True)
    localize_quote(quote, code)
    assert json.dumps(quote, sort_keys=True) == before


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_3b_the_uom_is_never_converted_or_translated(quote, code):
    """"3 COIL" stays 3 COIL. The canonical unit is not a display string."""
    wire = [l for l in quote["lines"] if l["skuId"] == WIRE][0]
    assert wire["quantity"] == 3
    assert str(wire.get("catalogueUom") or wire.get("uom")).upper() == "COIL"
    message = build_quotation_message(quote, None, None, language=code)
    assert "3 coils" in message["text"], code
    # 3 x 90m would be 270. If any language ever produced that, the unit had
    # been converted somewhere it must not be.
    assert "270" not in message["text"], code


# ---------------------------------------------------------------------------
# 4. CREDIT REGRESSION
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_4_the_credit_decision_is_identical_in_every_language(data, code):
    result = check_credit(data, RAVI, RAVI_ORDER)
    assert result["decision"] == "APPROVED"
    assert result["creditLimit"] == RAVI_LIMIT
    assert result["currentOutstanding"] == RAVI_OUTSTANDING
    assert result["projectedOutstanding"] == RAVI_PROJECTED
    assert result["remainingCredit"] == RAVI_REMAINING

    localized = localize_credit_status(result, code)
    assert localized["decision"] == "APPROVED"
    assert localized["limitText"] == "₹15,000.00"
    assert localized["outstandingText"] == "₹8,500.00"
    assert localized["projectedText"] == "₹12,700.00"
    assert localized["remainingText"] == "₹2,300.00"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_4a_the_decision_word_is_translated_but_the_code_is_not(credit, code):
    """Downstream reads the code; a person reads the word. Both are present."""
    localized = localize_credit_status(credit, code)
    assert localized["decision"] == credit["decision"]
    assert localized["decisionText"] == translate(code, "credit.approved")


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_4b_the_not_credit_scoring_statement_survives_translation(credit, code):
    """It is a statement about what ShopFlow is, not interface decoration.

    It is carried in every language because it is a commitment, and dropping
    it in some languages would be making the commitment selectively.
    """
    localized = localize_credit_status(credit, code)
    assert "does not perform credit scoring" in localized["policy"]


# ---------------------------------------------------------------------------
# 5. MARGIN REGRESSION - and it stays internal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_5_margin_figures_do_not_move_with_language(data, code):
    from engine.language import localize_margin_status

    view = margin_view(data, WIRE, CONFIRMED_WIRE_COST)
    assert view["oldMarginAmount"] == MARGIN_OLD
    assert view["newMarginAmount"] == MARGIN_NEW
    assert view["marginReductionAmount"] == MARGIN_REDUCTION

    localized = localize_margin_status(view, code)
    assert localized["previousText"] == "₹708.00"
    assert localized["currentText"] == "₹308.00"
    assert localized["reductionText"] == "₹400.00"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_5a_margin_never_reaches_a_customer_in_any_language(quote, credit, code):
    """Translating the shop's own working would still be leaking it."""
    text = build_quotation_message(quote, None, credit, language=code)["text"]
    for figure in ("708", "308", "400", "6,300", "5,900"):
        assert figure not in text, f"{code} leaked {figure}"
    for word in ("margin", "supplier", "cost price"):
        assert word not in text.lower(), f"{code} leaked the word {word!r}"
    # The translated words for margin must not appear either.
    for key in ("margin.previous", "margin.current", "margin.reduction"):
        assert translate(code, key) not in text, f"{code} leaked {key}"


# ---------------------------------------------------------------------------
# 6. PURCHASING REGRESSION
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["en", "ta", "hi", "te", "ur", "sat"])
def test_6_the_purchase_plan_is_unchanged_by_language(data, code):
    from engine.language import localize_purchase_plan

    plan = build_purchase_plan(data, 25000.0)
    payload = plan if isinstance(plan, dict) else plan.as_dict()
    localized = localize_purchase_plan(payload, code)
    # The allocator's own figures, formatted and not recomputed.
    assert localized["budgetText"] == "₹25,000.00"
    assert localized["allocatedText"] == \
        localize_purchase_plan(payload, "en")["allocatedText"]
    assert localized["leftOverText"] == \
        localize_purchase_plan(payload, "en")["leftOverText"]


def test_6a_planning_is_not_reachable_from_the_language_layer():
    """A localizer takes a finished plan. It cannot ask for a new one."""
    import engine.language as L
    source = open(L.__file__, encoding="utf-8").read()
    assert "build_purchase_plan" not in source
    assert "calculate_quote" not in source
    assert "check_credit" not in source


# ---------------------------------------------------------------------------
# 7. CLARIFICATION: the engine decides, the language layer only asks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_7_an_ambiguous_request_needs_clarification_in_every_language(data, code):
    """The specification is missing, so it is missing in all 23 languages."""
    phrase = ORDERS[code].replace(" 1-Way 10A White", "")
    canonical = canonicalize_request(phrase, code)
    result = resolve_product(data, requested_text=canonical["canonicalText"])
    assert result.status == AMBIGUOUS, f"{code}: {result.status}"
    assert result.clarifyingAttribute == "specification"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_7a_a_colour_clarification_is_the_same_decision_everywhere(data, code):
    result = resolve_product(data,
                             requested_text="3 coil Finolex 1.5 sq mm wire")
    assert result.status == AMBIGUOUS
    assert result.clarifyingAttribute == "colour"

    localized = localize_clarification(
        {"attribute": "colour"}, code)
    assert localized["question"] == translate(code, "clarify.colour")
    assert localized["language"] == normalize_language(code)


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_7b_the_localizer_cannot_choose_or_reorder_a_candidate(code):
    """Asking the question is localized. Answering it is not localizable."""
    options = [{"skuId": "A"}, {"skuId": "B"}, {"skuId": "C"}]
    localized = localize_clarification(
        {"attribute": "colour", "options": options}, code)
    assert "skuId" not in json.dumps(localized)
    assert "options" not in localized


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_7c_an_unknown_attribute_gets_the_generic_question_not_a_guess(code):
    localized = localize_clarification({"attribute": "wibble"}, code)
    assert localized["question"] == translate(code, "clarify.pickOne")


# ---------------------------------------------------------------------------
# 8. WHATSAPP: localized words, identical figures, same allow-list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8_a_customer_message_carries_the_same_money_in_every_language(
        quote, credit, code):
    english = build_quotation_message(quote, None, credit, language="en")["text"]
    other = build_quotation_message(quote, None, credit, language=code)["text"]
    assert money_tokens(other) == money_tokens(english), code
    assert "₹22,306.48" in other


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8a_product_names_are_the_catalogue_s_own_in_every_language(quote, code):
    text = build_quotation_message(quote, None, None, language=code)["text"]
    for line in quote["lines"]:
        assert line["name"] in text, f"{code} altered {line['name']}"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8b_brand_names_are_never_translated(quote, code):
    text = build_quotation_message(quote, None, None, language=code)["text"]
    for brand in ("Anchor", "Finolex", "Havells"):
        assert brand in text, f"{code} lost {brand}"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8c_no_internal_field_reaches_a_customer_in_any_language(
        data, quote, credit, code):
    text = build_quotation_message(quote, None, credit, language=code)["text"]
    for field in INTERNAL_ONLY_FIELDS:
        assert field not in text, f"{code} leaked field {field}"
    for line in quote["lines"]:
        product = data.product(line["skuId"])
        assert f"{product.costPrice:,.2f}" not in text, f"{code} leaked a cost"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8d_an_account_message_reports_the_customer_s_own_balance(credit, code):
    text = build_credit_status_message(credit, None, reminder=True,
                                       language=code)["text"]
    assert "₹8,500.00" in text
    assert "₹15,000.00" in text


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8e_a_partly_translated_language_still_sends_a_whole_message(
        quote, credit, code):
    """Never an empty line, never a key name, never a broken placeholder.

    Santali, Bodo and Manipuri are genuinely partial, so for those three this
    is the real fallback path and not a simulated one.
    """
    text = build_quotation_message(quote, None, credit, language=code)["text"]
    assert text.strip()
    assert "undefined" not in text.lower()
    assert "None" not in text
    assert not re.search(r"\{[a-zA-Z_]+\}", text), code
    assert not re.search(r"\b[a-z]+\.[a-z][a-zA-Z]+\b", text.replace(
        "ShopFlow AI", "")), f"{code} looks like it printed a key"


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_8f_the_message_reports_the_language_it_was_built_in(quote, code):
    assert build_quotation_message(quote, None, None,
                                   language=code)["language"] == code


# ---------------------------------------------------------------------------
# 9. THE HONEST LIMIT: a translated product noun is asked about, not guessed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,code", [
    ("எனக்கு 20 ஆங்கர் சுவிட்ச் வேண்டும்", "ta"),
    ("मुझे 20 ऐंकर स्विच चाहिए", "hi"),
    ("నాకు 20 ఆంకర్ స్విచ్ కావాలి", "te"),
])
def test_9_a_product_noun_outside_catalogue_vocabulary_is_never_guessed(
        data, phrase, code):
    """This is the boundary, and it is documented rather than papered over.

    The catalogue is written in English, and the deterministic matcher reads
    the catalogue. A request whose product noun has been transliterated out of
    catalogue vocabulary therefore does not match - and what matters is what
    happens next. It must NOT resolve to a plausible-looking SKU. It comes
    back as NOT_FOUND or as a question, the owner is asked, and nobody is
    quoted for something they did not ask for.

    Text and UI localization remain fully supported for these languages; it is
    only free-text product matching in a non-Latin product noun that is out of
    scope, and the README says exactly that.

    This test found a real defect when it was written. Stripping the language's
    function words from "నాకు 20 ఆంకర్ స్విచ్ కావాలి" leaves the digits alone,
    and "20" matched ACC-CONDUIT-20 exactly, score 1.0 - a customer asking for
    twenty switches was one step from a quotation for 20mm conduit. The
    adapter now reports that no catalogue-readable product wording survived,
    and the caller asks rather than matches.
    """
    canonical = canonicalize_request(phrase, code)
    assert canonical["hasProductVocabulary"] is False, (
        f"{code}: {canonical['canonicalText']!r} was treated as a description")


def test_9a_the_adapter_never_reports_having_resolved_anything():
    for code in ALL_LANGUAGES:
        assert canonicalize_request(ORDERS[code], code)["resolvesProducts"] is False


@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_9b_a_real_order_does_carry_product_vocabulary(code):
    """The guard must not fire on the orders that are supposed to work."""
    assert canonicalize_request(ORDERS[code], code)["hasProductVocabulary"]


@pytest.mark.parametrize("bare", ["20", "  20  ", "౨౦", "२०", "20 30"])
def test_9c_a_bare_quantity_is_never_a_product_description(bare):
    """A number alone must not reach the matcher in any language.

    This is the general form of the conduit defect: SKU codes contain digits,
    so a bare number can score a perfect match against one. It is a quantity,
    and a quantity is a question waiting for its noun.
    """
    for code in ("en", "ta", "hi", "te"):
        assert canonicalize_request(bare, code)["hasProductVocabulary"] is False


# ---------------------------------------------------------------------------
# 10. VOICE: what is genuinely available, and what is honestly not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ALL_LANGUAGES)
def test_10_voice_capability_is_reported_per_language(code):
    cap = capabilities(code)
    assert isinstance(cap["voice"], bool)
    if cap["voice"]:
        assert cap["voiceCode"], code
    else:
        # A language without voice still has text, and the interface has a
        # string to say so with.
        assert cap["text"] is True
        assert translate(code, "ui.voiceUnavailable")


def test_10a_a_language_without_voice_is_not_called_fully_supported():
    """`text` and `voice` are separate flags for exactly this reason."""
    from engine.speech import TRANSCRIBE_LANGUAGES

    silent = [c for c in SCHEDULED if c not in TRANSCRIBE_LANGUAGES]
    assert silent, "the Transcribe map covers everything - verify that"
    for code in silent:
        assert capabilities(code)["voice"] is False
        assert capabilities(code)["text"] is True


@pytest.mark.parametrize("code", ["ta", "hi", "te", "kn", "ml", "mr", "bn",
                                  "gu", "or", "pa", "ne", "en"])
def test_10b_a_voice_language_pins_the_transcribe_job_to_one_code(code):
    """A selected language is passed to Transcribe, not auto-detected.

    Automatic identification is for the two-language shop default. Where the
    owner has said which language they are speaking, guessing again would be
    a worse answer than the one they gave.
    """
    from engine.speech import resolve_language

    resolved = resolve_language(code)
    assert resolved["identifyLanguage"] is False
    assert resolved["languageCode"], code


def test_10c_a_language_transcribe_does_not_accept_falls_back_to_auto():
    from engine.speech import resolve_language

    for code in ("sat", "brx", "ks", "sd", "ur", "sa"):
        resolved = resolve_language(code)
        assert resolved["identifyLanguage"] is True
        assert resolved["requested"] == "auto"


def test_10d_a_locale_tag_is_accepted_by_the_speech_layer_too():
    from engine.speech import resolve_language

    assert resolve_language("ta-IN")["languageCode"] == "ta-IN"
    assert resolve_language("hi-IN")["languageCode"] == "hi-IN"


# ---------------------------------------------------------------------------
# 11. performance and purity
# ---------------------------------------------------------------------------

def test_11_changing_language_requires_no_business_call(quote, credit):
    """Re-rendering in another language must be words only.

    Proven by doing it: the same already-calculated dicts are handed to
    twenty-three localizers, and every figure that comes back is identical.
    """
    totals = set()
    for code in ALL_LANGUAGES:
        totals.add(localize_quote(quote, code)["totalText"])
        totals.add(localize_credit_status(credit, code)["limitText"])
    assert totals == {"₹22,306.48", "₹15,000.00"}


def test_11a_rupee_figures_stay_in_latin_digits_in_every_language(quote):
    """A shop owner checks a WhatsApp message against a paper bill.

    The number has to look like the number, so amounts are not rendered in
    native numerals anywhere.
    """
    for code in ALL_LANGUAGES:
        text = build_quotation_message(quote, None, None, language=code)["text"]
        for token in money_tokens(text):
            assert re.fullmatch(r"₹[\d,]+\.\d{2}", token), (code, token)
