"""The language layer: the registry, the resources, and what they must not do.

WHAT IS BEING CHECKED
---------------------
Two separate things, and it is worth keeping them apart.

The first is that the registry is correct: 22 Scheduled Languages, English
listed separately and never counted among them, real ISO codes, native names
in the script each resource file is actually written in, and direction read
from metadata rather than guessed from a code.

The second, and the one that matters more, is that the layer cannot reach the
business. There is no arithmetic here, no SKU, no unit conversion and no
credit comparison - several tests below assert that structurally, by reading
the module rather than by trusting the docstring.

The business-equivalence tests - same order, twenty-three languages, one
quotation - live in test_multilingual.py.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re
import subprocess
import sys

import pytest

from engine import language as L

ROOT = pathlib.Path(__file__).resolve().parents[1]
I18N = ROOT / "backend" / "i18n"

# The Eighth Schedule, as the Constitution lists it. Written out here rather
# than imported so the test is an independent statement of the requirement.
EIGHTH_SCHEDULE = {
    "as": "Assamese", "bn": "Bengali", "brx": "Bodo", "doi": "Dogri",
    "gu": "Gujarati", "hi": "Hindi", "kn": "Kannada", "ks": "Kashmiri",
    "kok": "Konkani", "mai": "Maithili", "ml": "Malayalam", "mni": "Manipuri",
    "mr": "Marathi", "ne": "Nepali", "or": "Odia", "pa": "Punjabi",
    "sa": "Sanskrit", "sat": "Santali", "sd": "Sindhi", "ta": "Tamil",
    "te": "Telugu", "ur": "Urdu",
}

# Unicode blocks, so "the native name is in the right script" is checked
# against the characters rather than against my eyesight.
SCRIPT_RANGES = {
    "Devanagari": (0x0900, 0x097F),
    "Bengali-Assamese": (0x0980, 0x09FF),
    "Bengali": (0x0980, 0x09FF),
    "Gurmukhi": (0x0A00, 0x0A7F),
    "Gujarati": (0x0A80, 0x0AFF),
    "Odia": (0x0B00, 0x0B7F),
    "Tamil": (0x0B80, 0x0BFF),
    "Telugu": (0x0C00, 0x0C7F),
    "Kannada": (0x0C80, 0x0CFF),
    "Malayalam": (0x0D00, 0x0D7F),
    "Ol Chiki": (0x1C50, 0x1C7F),
    "Perso-Arabic": (0x0600, 0x06FF),
    "Latin": (0x0041, 0x007A),
}


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

def test_1_all_twenty_two_scheduled_languages_are_present():
    assert set(L.scheduled_languages()) == set(EIGHTH_SCHEDULE)
    assert len(L.scheduled_languages()) == 22


def test_1a_english_is_supported_but_is_not_one_of_the_twenty_two():
    """The claim is "22 Scheduled Languages, with English as the fallback".

    English is not in the Eighth Schedule, and a product that quietly counted
    it as the twenty-third would be making a false statement about the
    Constitution in its own marketing.
    """
    assert "en" in L.supported_languages()
    assert "en" not in L.scheduled_languages()
    assert L.is_scheduled("en") is False
    assert L.DEFAULT_LANGUAGE == "en"
    assert len(L.supported_languages()) == 23


def test_1b_every_code_is_the_iso_code_for_that_language():
    for code, name in EIGHTH_SCHEDULE.items():
        assert L.get_language_metadata(code)["name"] == name


def test_2_every_language_has_a_native_name_in_its_own_script():
    for code in L.scheduled_languages():
        meta = L.get_language_metadata(code)
        native = meta["nativeName"]
        assert native and native.strip(), code
        low, high = SCRIPT_RANGES[meta["script"]]
        assert any(low <= ord(ch) <= high for ch in native), (
            f"{code}: native name {native!r} is not in {meta['script']}")


def test_2a_the_native_label_uses_the_same_script_as_the_resource_file():
    """A selector label in one script and a translation in another confuses.

    Manipuri and Konkani each have more than one living script convention.
    Whichever is chosen, the label the owner clicks and the words they then
    read have to be the same one.
    """
    for code in L.scheduled_languages():
        meta = L.get_language_metadata(code)
        low, high = SCRIPT_RANGES[meta["script"]]
        resource = json.loads((I18N / f"{code}.json").read_text(encoding="utf-8"))
        text = json.dumps(resource, ensure_ascii=False)
        translated = [ch for ch in text if ord(ch) > 0x0590]
        if not translated:
            continue
        in_script = sum(1 for ch in translated if low <= ord(ch) <= high)
        assert in_script / len(translated) > 0.9, (
            f"{code}: resource is not mostly {meta['script']}")


def test_2b_a_language_with_more_than_one_script_says_which_one_it_uses():
    for code in ("ks", "kok", "mni", "sd", "sat", "brx"):
        assert L.get_language_metadata(code).get("scriptNote"), code


# ---------------------------------------------------------------------------
# direction
# ---------------------------------------------------------------------------

def test_3_direction_comes_from_metadata_not_from_the_code():
    assert L.direction("ur") == L.RTL
    assert L.direction("sd") == L.RTL
    assert L.direction("ks") == L.RTL
    for code in ("ta", "hi", "bn", "ml", "en", "sat", "mni"):
        assert L.direction(code) == L.LTR, code


def test_3a_rtl_is_not_applied_globally():
    rtl = [c for c in L.supported_languages() if L.is_rtl(c)]
    assert set(rtl) == {"ur", "sd", "ks"}


def test_3b_every_language_declares_a_direction_explicitly():
    for code in L.supported_languages():
        assert L.get_language_metadata(code)["direction"] in (L.LTR, L.RTL)


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("ta", "ta"), ("ta-IN", "ta"), ("TA", "ta"), ("ta_IN", "ta"),
    ("hi-Latn-IN", "hi"), ("EN-GB", "en"),
])
def test_4_a_locale_tag_reduces_to_a_code(value, expected):
    assert L.normalize_language(value) == expected


@pytest.mark.parametrize("value", ["", None, "klingon", "xx", "   ", 42, "tam"])
def test_4a_an_unknown_tag_falls_back_to_english_rather_than_failing(value):
    """A presentation preference must not be able to fail a business request.

    A browser sending a locale ShopFlow does not know should get an English
    quotation, not a 400. "tam" is in this list on purpose: it is a real code
    for Tamil in another standard, and guessing at it is how a customer ends
    up reading a message in somebody else's language.
    """
    assert L.normalize_language(value) == "en"


def test_4b_strict_mode_exists_for_callers_that_need_to_know():
    with pytest.raises(L.UnknownLanguageError):
        L.normalize_language("klingon", strict=True)


# ---------------------------------------------------------------------------
# resources and fallback
# ---------------------------------------------------------------------------

def test_5_every_supported_language_has_a_resource_file():
    for code in L.supported_languages():
        assert (I18N / f"{code}.json").exists(), code


def test_5a_english_defines_the_contract_and_it_is_not_empty():
    keys = L.translation_keys()
    assert len(keys) >= 50
    assert "quote.total" in keys
    assert "credit.approved" in keys
    assert "clarify.colour" in keys


def test_5b_no_language_invents_a_key_english_does_not_have():
    """An extra key is a string nothing will ever read - dead, and misleading.

    It also means the two files have drifted, which is the failure this
    catches before a reviewer has to notice it by eye.
    """
    english = set(L.translation_keys())
    for code in L.scheduled_languages():
        mine = set(L._resource(code))
        assert not (mine - english), f"{code}: unknown keys {sorted(mine - english)}"


def test_6_a_missing_translation_falls_back_to_english():
    # Santali is genuinely partial, so this is real behaviour and not a mock.
    assert not L.has_translation("sat", "quote.notInvoice")
    assert L.translate("sat", "quote.notInvoice") == \
        L.translate("en", "quote.notInvoice")


def test_6a_fallback_is_never_undefined_null_or_a_key_name():
    for code in L.supported_languages():
        for key in L.translation_keys():
            value = L.translate(code, key)
            assert value, f"{code}/{key} is empty"
            assert value.lower() not in ("undefined", "null", "none")
            assert value != key
            assert "{" not in value or "}" in value


def test_6b_an_unknown_key_returns_nothing_rather_than_leaking_its_name():
    """An internal key name in a customer's WhatsApp message is a bug escaping.

    Empty is the safe answer: the caller's own fallback text takes over.
    """
    assert L.translate("ta", "no.such.key.exists") == ""


def test_7_coverage_is_measured_from_the_files_not_declared():
    assert L.coverage("en") == 1.0
    assert L.coverage("ta") == 1.0
    assert L.coverage("hi") == 1.0
    # Honestly partial, and reported as such rather than rounded up.
    assert 0 < L.coverage("sat") < 1
    assert 0 < L.coverage("brx") < 1
    assert 0 < L.coverage("mni") < 1


def test_7a_no_language_claims_native_speaker_review():
    """The one claim this repository must never make without having done it."""
    for code in L.scheduled_languages():
        assert L.capabilities(code)["reviewed"] is False, code
        meta = L.resource_meta(code)
        assert meta.get("reviewed") is False, code
        assert "native" in meta.get("note", "").lower()


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------

def test_8_a_translation_that_loses_a_placeholder_is_rejected(monkeypatch):
    """Losing `{total}` would send a quotation with no amount in it.

    English is used instead, which is a worse-reading message and a correct
    one. That trade is the right way round.
    """
    monkeypatch.setitem(L._resource("en"), "test.placeholder", "Total: {total}")
    monkeypatch.setitem(L._resource("ta"), "test.placeholder", "மொத்தம்")
    assert L.translate("ta", "test.placeholder", total="1") == "Total: 1"


def test_8a_a_matching_placeholder_is_substituted():
    L._resource("en")["test.ok"] = "Total: {total}"
    L._resource("ta")["test.ok"] = "மொத்தம்: {total}"
    try:
        assert L.translate("ta", "test.ok", total="₹22,306.48") == \
            "மொத்தம்: ₹22,306.48"
    finally:
        L._resource("en").pop("test.ok", None)
        L._resource("ta").pop("test.ok", None)


def test_8b_an_unsupplied_placeholder_is_left_alone_rather_than_raising():
    L._resource("en")["test.missing"] = "Total: {total}"
    try:
        assert L.translate("en", "test.missing") == "Total: {total}"
    finally:
        L._resource("en").pop("test.missing", None)


def test_8c_no_shipped_string_uses_a_placeholder_english_does_not():
    english = L._resource("en")
    pattern = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
    for code in L.scheduled_languages():
        for key, text in L._resource(code).items():
            assert set(pattern.findall(text)) == set(
                pattern.findall(english.get(key, ""))), f"{code}/{key}"


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------

def test_9_the_capability_matrix_covers_every_language():
    matrix = L.capability_matrix()
    assert set(matrix) == set(L.supported_languages())
    for code, cap in matrix.items():
        assert set(cap) >= {"text", "ui", "voice", "whatsapp", "coverage",
                            "confidence", "reviewed"}


def test_9a_voice_is_read_from_the_speech_provider_not_assumed():
    """`voice: true` has to come from the provider's own language map.

    A language is voice-capable here because Amazon Transcribe accepts its
    code, which is verified against botocore's service model by the
    multilingual smoke test. Nothing declares it by hand.
    """
    from engine.speech import TRANSCRIBE_LANGUAGES
    for code in L.supported_languages():
        assert L.capabilities(code)["voice"] is (code in TRANSCRIBE_LANGUAGES)


def test_9b_text_support_is_universal_and_voice_support_is_not():
    """The distinction the interface has to make, asserted here.

    Every language can be typed. Fewer can be spoken. A product that called
    both "supported" would be overstating half of them.
    """
    matrix = L.capability_matrix()
    assert all(c["text"] for c in matrix.values())
    voice = {code for code, c in matrix.items() if c["voice"]}
    assert voice, "no language has voice - the map is broken"
    assert len(voice) < len(matrix), "every language claims voice - suspicious"
    for code in ("ks", "sat", "mni", "brx", "doi", "kok", "mai", "sa", "sd",
                 "ur", "as"):
        assert code not in voice, f"{code} claims voice it does not have"


def test_9c_a_voice_language_carries_the_provider_code_it_maps_to():
    for code in L.voice_languages():
        assert L.capabilities(code)["voiceCode"], code


# ---------------------------------------------------------------------------
# canonical input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("native,expected", [
    ("२०", "20"), ("௨௦", "20"), ("২০", "20"), ("౨౦", "20"),
    ("೨೦", "20"), ("൨൦", "20"), ("୨୦", "20"), ("૨૦", "20"),
    ("੨੦", "20"), ("٢٠", "20"), ("۲۰", "20"),
])
def test_10_native_digits_become_latin_digits(native, expected):
    assert L.normalize_digits(native) == expected


def test_10a_normalising_digits_touches_nothing_else():
    text = "Anchor 1-Way 10A White"
    assert L.normalize_digits(text) == text


def test_10b_the_adapter_strips_function_words_and_leaves_the_product():
    result = L.canonicalize_request("எனக்கு 20 Anchor switch வேண்டும்", "ta")
    assert "Anchor" in result["canonicalText"]
    assert "20" in result["canonicalText"]
    assert "வேண்டும்" not in result["canonicalText"]


def test_10c_brand_names_survive_every_language():
    """Anchor is Anchor in all twenty-three. The catalogue owns that word."""
    for code in L.supported_languages():
        out = L.canonicalize_request("20 Anchor Finolex Havells switch", code)
        for brand in ("Anchor", "Finolex", "Havells"):
            assert brand in out["canonicalText"], f"{code} lost {brand}"


def test_10d_the_adapter_says_of_itself_that_it_resolves_no_product():
    assert L.canonicalize_request("anything", "ta")["resolvesProducts"] is False


# ---------------------------------------------------------------------------
# what the layer must not be able to do
# ---------------------------------------------------------------------------

def test_11_the_language_module_contains_no_business_arithmetic():
    """Read structurally, not by reading the prose at the top of the file.

    A localizer that could add, multiply or compare could produce a number the
    engine never calculated. There is no reason for one operator of that kind
    to appear in this module, so none is allowed.
    """
    source = (ROOT / "backend" / "engine" / "language.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # The functions that touch a business result are the ones that matter.
    # Elsewhere in the module arithmetic is legitimate - joining a path,
    # counting translation keys, walking a Unicode block - so the rule is
    # applied where a stray operator could actually produce a wrong rupee
    # figure: inside the localizers themselves.
    localizers = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and (n.name.startswith("localize_") or n.name == "_money")]
    assert len(localizers) >= 6, "the localizers moved - update this test"

    for fn in localizers:
        for node in ast.walk(fn):
            assert not isinstance(node, ast.BinOp), (
                f"{fn.name} contains arithmetic at line {node.lineno}")
            if isinstance(node, ast.Compare):
                # `x is None` is a null guard, not a judgement about a value.
                # Any other comparison would mean the localizer had started
                # deciding something, which is the engine's job.
                assert all(isinstance(op, (ast.Is, ast.IsNot))
                           for op in node.ops), (
                    f"{fn.name} compares values at line {node.lineno}")
                assert all(isinstance(c, ast.Constant) and c.value is None
                           for c in node.comparators), (
                    f"{fn.name} compares against a value at line {node.lineno}")


def test_11a_the_language_module_never_calls_the_business_engines():
    source = (ROOT / "backend" / "engine" / "language.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden = {"quote", "credit", "purchasing", "margin", "uom", "pricing",
                 "budget", "matching", "cost_records", "shortage",
                 ".quote", ".credit", ".purchasing", ".margin", ".uom",
                 ".pricing", ".budget", ".matching", ".cost_records"}
    assert not (imported & forbidden), imported & forbidden


def test_11b_localizing_a_response_mutates_nothing():
    original = {"quote": {"total": 22306.48, "lines": [{"name": "x"}]},
                "status": "QUOTED"}
    snapshot = json.dumps(original, sort_keys=True)
    out = L.localize_response(original, "ta")
    assert json.dumps(original, sort_keys=True) == snapshot
    assert out["quote"] is original["quote"]
    assert out["status"] == "QUOTED"


def test_11c_no_localize_function_accepts_a_figure_to_calculate_with():
    """Every localizer takes a RESULT, never the inputs to produce one.

    A signature like `localize_credit(limit, outstanding)` would mean the
    words and the arithmetic had been put in the same place. They have not.
    """
    for name in ("localize_quote", "localize_credit_status",
                 "localize_margin_status", "localize_purchase_plan"):
        params = list(inspect.signature(getattr(L, name)).parameters)
        assert params[0] in ("quote", "credit", "margin", "plan"), name
        assert params[1] == "code", name
        assert len(params) == 2, name


# ---------------------------------------------------------------------------
# resources are bundled with both things that need them
# ---------------------------------------------------------------------------

def test_12_the_site_copy_of_the_resources_is_in_sync():
    """One source of truth, mechanically enforced.

    The Lambda reads backend/i18n and the browser fetches
    frontend/site/i18n, because the two bundles cannot reach each other. The
    copy is made by a script and checked here, so the two can never quietly
    become two different translations.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_i18n.py"), "--check"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_12a_the_resources_are_local_files_not_a_remote_service():
    """No translation API, by construction.

    The prompt for this work asked for deterministic resources rather than a
    translation service, and this is what makes that true rather than stated:
    the module reads JSON off the filesystem and has no client of any kind.
    """
    source = (ROOT / "backend" / "engine" / "language.py").read_text(encoding="utf-8")
    for forbidden in ("boto3", "translate_text", "urllib", "requests",
                      "http.client", "bedrock"):
        assert forbidden not in source, forbidden
