"""The language layer: how the shop talks, never what ShopFlow decides.

WHAT THIS MODULE IS
-------------------
One source of truth for the languages ShopFlow speaks - India's 22 Scheduled
Languages, with English as the default and the fallback - and a deterministic
renderer for the words around a business result.

It does four things:

  normalize_language     an arbitrary tag ("ta-IN", "TA", None) -> a code
  get_language_metadata  name, native name, direction, script
  capabilities           what each language can actually do here, today
  translate / localize_* the words, from bundled resource files

WHAT IT IS NOT
--------------
It owns no business meaning. It resolves no SKU, converts no unit, compares no
credit limit and calculates no money. Every figure that appears in a localized
string was interpolated from a value the deterministic engine had already
produced; there is no arithmetic in this file beyond digit-grouping, which is
delegated to `engine.messages.format_rupees` so a rupee figure reads the same
however it leaves the building.

That is the whole point of the layer:

    different language -> same canonical meaning -> same business result.

The language changes HOW the owner and the customer communicate. It must never
change WHAT the shop decides.

TRANSLATION QUALITY, STATED HONESTLY
------------------------------------
Every resource file in `backend/i18n/` is an application-provided translation.
None of it has been reviewed by a native speaker. Each file carries its own
confidence mark, and `coverage()` reports - from the files themselves, not
from a claim - how much of each language is actually translated. Where a key
is missing the English string is used, because a customer reading an English
sentence is served better than one reading a broken key.

SCRIPTS
-------
Direction is read from metadata, never guessed from the code: Urdu, Kashmiri
and Sindhi are written here in Perso-Arabic script and are right-to-left, and
the rest are left-to-right. Where a language has more than one living script
convention the choice is recorded in `script` and explained in `scriptNote`,
and the native label in the selector is written in the same script as the
resource file it belongs to.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

from .messages import format_rupees

I18N_DIR = Path(__file__).resolve().parents[1] / "i18n"

# The application's default and its fallback. English is NOT one of the 22.
DEFAULT_LANGUAGE = "en"

LTR = "ltr"
RTL = "rtl"

# Confidence marks. These describe how much trust to place in the wording, not
# whether the plumbing works - the plumbing is the same for all of them.
HIGH = "high"
MODERATE = "moderate"
LOW = "low"

REVIEW_NOTICE = ("Application-provided translation; native-language review "
                 "recommended before production deployment.")

# ---------------------------------------------------------------------------
# the registry - one source of truth
# ---------------------------------------------------------------------------
# `scheduled` marks the 22 languages of the Eighth Schedule to the
# Constitution of India. English is present, and is deliberately not one of
# them: it is this application's default and fallback language.

_REGISTRY: Dict[str, dict] = {
    "as": {"name": "Assamese", "nativeName": "অসমীয়া", "direction": LTR,
           "script": "Bengali-Assamese", "scheduled": True},
    "bn": {"name": "Bengali", "nativeName": "বাংলা", "direction": LTR,
           "script": "Bengali-Assamese", "scheduled": True},
    "brx": {"name": "Bodo", "nativeName": "बड़ो", "direction": LTR,
            "script": "Devanagari", "scheduled": True,
            "scriptNote": "Bodo is written in Devanagari, which is its "
                          "official script."},
    "doi": {"name": "Dogri", "nativeName": "डोगरी", "direction": LTR,
            "script": "Devanagari", "scheduled": True},
    "gu": {"name": "Gujarati", "nativeName": "ગુજરાતી", "direction": LTR,
           "script": "Gujarati", "scheduled": True},
    "hi": {"name": "Hindi", "nativeName": "हिन्दी", "direction": LTR,
           "script": "Devanagari", "scheduled": True},
    "kn": {"name": "Kannada", "nativeName": "ಕನ್ನಡ", "direction": LTR,
           "script": "Kannada", "scheduled": True},
    "ks": {"name": "Kashmiri", "nativeName": "کٲشُر", "direction": RTL,
           "script": "Perso-Arabic", "scheduled": True,
           "scriptNote": "Kashmiri is written in both Perso-Arabic and "
                         "Devanagari. The Perso-Arabic convention is used "
                         "here, so this language is right-to-left."},
    "kok": {"name": "Konkani", "nativeName": "कोंकणी", "direction": LTR,
            "script": "Devanagari", "scheduled": True,
            "scriptNote": "Konkani is written in several scripts. Devanagari "
                          "is used here, as the official script in Goa."},
    "mai": {"name": "Maithili", "nativeName": "मैथिली", "direction": LTR,
            "script": "Devanagari", "scheduled": True},
    "ml": {"name": "Malayalam", "nativeName": "മലയാളം", "direction": LTR,
           "script": "Malayalam", "scheduled": True},
    "mni": {"name": "Manipuri", "nativeName": "মৈতৈলোন্", "direction": LTR,
            "script": "Bengali", "scheduled": True,
            "scriptNote": "Meitei Mayek (ꯃꯤꯇꯩꯂꯣꯟ) is the official script for "
                          "Manipuri. The Bengali script convention is used "
                          "here because the translation resource is written "
                          "in it; the selector label matches the resource."},
    "mr": {"name": "Marathi", "nativeName": "मराठी", "direction": LTR,
           "script": "Devanagari", "scheduled": True},
    "ne": {"name": "Nepali", "nativeName": "नेपाली", "direction": LTR,
           "script": "Devanagari", "scheduled": True},
    "or": {"name": "Odia", "nativeName": "ଓଡ଼ିଆ", "direction": LTR,
           "script": "Odia", "scheduled": True},
    "pa": {"name": "Punjabi", "nativeName": "ਪੰਜਾਬੀ", "direction": LTR,
           "script": "Gurmukhi", "scheduled": True},
    "sa": {"name": "Sanskrit", "nativeName": "संस्कृतम्", "direction": LTR,
           "script": "Devanagari", "scheduled": True},
    "sat": {"name": "Santali", "nativeName": "ᱥᱟᱱᱛᱟᱲᱤ", "direction": LTR,
            "script": "Ol Chiki", "scheduled": True,
            "scriptNote": "Ol Chiki is the official script for Santali and is "
                          "used here."},
    "sd": {"name": "Sindhi", "nativeName": "سنڌي", "direction": RTL,
           "script": "Perso-Arabic", "scheduled": True,
           "scriptNote": "Sindhi is written in both Perso-Arabic and "
                         "Devanagari. The Perso-Arabic convention is used "
                         "here, so this language is right-to-left."},
    "ta": {"name": "Tamil", "nativeName": "தமிழ்", "direction": LTR,
           "script": "Tamil", "scheduled": True},
    "te": {"name": "Telugu", "nativeName": "తెలుగు", "direction": LTR,
           "script": "Telugu", "scheduled": True},
    "ur": {"name": "Urdu", "nativeName": "اردو", "direction": RTL,
           "script": "Perso-Arabic", "scheduled": True},
    # Not one of the 22. The application's default and fallback.
    "en": {"name": "English", "nativeName": "English", "direction": LTR,
           "script": "Latin", "scheduled": False},
}

SCHEDULED_LANGUAGES: Tuple[str, ...] = tuple(
    code for code, meta in _REGISTRY.items() if meta["scheduled"])

SUPPORTED_LANGUAGES: Tuple[str, ...] = tuple(_REGISTRY)

# Native-script digits, so "२०" and "௨௦" reach the matcher as "20". This is
# transliteration of a numeral, not interpretation of a quantity: the engine
# still parses, validates and uses the number, exactly as it does for a number
# that arrived in Latin digits.
_DIGIT_MAP = {}
for _base in (
    0x0966,  # Devanagari - hi, mr, ne, sa, brx, doi, kok, mai
    0x09E6,  # Bengali-Assamese - bn, as, mni
    0x0A66,  # Gurmukhi - pa
    0x0AE6,  # Gujarati - gu
    0x0B66,  # Odia - or
    0x0BE6,  # Tamil
    0x0C66,  # Telugu
    0x0CE6,  # Kannada
    0x0D66,  # Malayalam
    0x0660,  # Arabic-Indic - ur, ks, sd
    0x06F0,  # Extended Arabic-Indic - ur, sd
    0x1C50,  # Ol Chiki - sat
):
    for _offset in range(10):
        _DIGIT_MAP[chr(_base + _offset)] = str(_offset)

_PLACEHOLDER = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")


class UnknownLanguageError(ValueError):
    """The language tag is not one ShopFlow supports."""


# ---------------------------------------------------------------------------
# the registry, read
# ---------------------------------------------------------------------------

def supported_languages() -> Tuple[str, ...]:
    """Every code this application accepts, the 22 plus English."""
    return SUPPORTED_LANGUAGES


def scheduled_languages() -> Tuple[str, ...]:
    """The 22 Scheduled Languages. English is not among them, by definition."""
    return SCHEDULED_LANGUAGES


def is_scheduled(code: str) -> bool:
    return bool(_REGISTRY.get(str(code or "").lower(), {}).get("scheduled"))


def normalize_language(value, *, strict: bool = False) -> str:
    """Any reasonable tag, reduced to a code this application knows.

    "ta-IN", "TA", "ta_IN" and "ta" all mean Tamil. Anything unrecognised
    becomes English rather than an error, because a customer seeing English is
    a far better outcome than a request that fails over a locale string. Pass
    `strict=True` where a caller genuinely needs to know it was wrong.
    """
    raw = str(value or "").strip()
    if not raw:
        if strict:
            raise UnknownLanguageError("no language given")
        return DEFAULT_LANGUAGE

    primary = re.split(r"[-_]", raw)[0].lower()
    if primary in _REGISTRY:
        return primary
    # A three-letter code that happens to be written in full ("tam", "hin")
    # is not guessed at: guessing a language is how a customer ends up reading
    # a message in somebody else's.
    if strict:
        raise UnknownLanguageError(f"unsupported language: {raw[:20]}")
    return DEFAULT_LANGUAGE


def get_language_metadata(code: str) -> dict:
    """Name, native name, direction and script for one language."""
    resolved = normalize_language(code)
    meta = dict(_REGISTRY[resolved])
    meta["code"] = resolved
    meta.update(capabilities(resolved))
    return meta


def language_directory() -> list:
    """Every language, in the order the selector should show them.

    English first because it is the default, then the 22 in alphabetical order
    of their English names, which is the order the Eighth Schedule itself uses.
    """
    ordered = [DEFAULT_LANGUAGE] + sorted(
        SCHEDULED_LANGUAGES, key=lambda c: _REGISTRY[c]["name"])
    return [get_language_metadata(code) for code in ordered]


def direction(code: str) -> str:
    """Text direction, from metadata. Never inferred from the code itself."""
    return _REGISTRY[normalize_language(code)]["direction"]


def is_rtl(code: str) -> bool:
    return direction(code) == RTL


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------

@lru_cache(maxsize=len(_REGISTRY))
def _resource(code: str) -> dict:
    """One language's flattened resource file. Cached per process.

    A missing or unreadable file is not fatal. It means that language falls
    back to English entirely, which is a degraded experience rather than an
    outage, and `coverage()` will report it as 0.
    """
    path = I18N_DIR / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _flatten(raw)


def _flatten(tree: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in (tree or {}).items():
        if key.startswith("_"):
            continue
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        elif isinstance(value, str) and value.strip():
            flat[path] = value
    return flat


def resource_meta(code: str) -> dict:
    """The `_meta` block of a resource file: confidence, script, review state."""
    path = I18N_DIR / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw.get("_meta") or {}


def translation_keys() -> Tuple[str, ...]:
    """Every key English defines. English is the contract."""
    return tuple(sorted(_resource(DEFAULT_LANGUAGE)))


def coverage(code: str) -> float:
    """How much of English this language actually translates, 0.0 to 1.0.

    Measured from the files, not asserted. A partial file is a legitimate
    state - the untranslated keys fall back to English - and this is how the
    interface can say so instead of implying the language is finished.
    """
    resolved = normalize_language(code)
    english = _resource(DEFAULT_LANGUAGE)
    if not english:
        return 0.0
    if resolved == DEFAULT_LANGUAGE:
        return 1.0
    mine = _resource(resolved)
    present = sum(1 for key in english if key in mine)
    return round(present / len(english), 4)


# ---------------------------------------------------------------------------
# translation
# ---------------------------------------------------------------------------

def _placeholders(text: str) -> frozenset:
    return frozenset(_PLACEHOLDER.findall(text or ""))


def translate(code: str, key: str, **params) -> str:
    """One string, in the requested language, with English behind it.

    Three rules, each of which exists because the alternative reaches a
    customer:

      1. A missing key falls back to English. Never "undefined", never the
         key itself, never an empty message.
      2. A translation whose placeholders do not match English is REJECTED in
         favour of English. A localized string that lost `{total}` would send
         a quotation with no amount in it, which is worse than sending it in
         English.
      3. Substitution never raises. An unknown placeholder is left as written
         rather than taking the request down.
    """
    resolved = normalize_language(code)
    english = _resource(DEFAULT_LANGUAGE).get(key)
    text = _resource(resolved).get(key) if resolved != DEFAULT_LANGUAGE else english

    if text is None:
        text = english
    elif english is not None and _placeholders(text) != _placeholders(english):
        text = english

    if text is None:
        # No English either: the key does not exist. Return nothing rather
        # than leaking an internal key name into a customer's message.
        return ""

    def substitute(match):
        name = match.group(1)
        return str(params[name]) if name in params else match.group(0)

    return _PLACEHOLDER.sub(substitute, text)


def has_translation(code: str, key: str) -> bool:
    """Whether this language translates that key itself, without fallback."""
    resolved = normalize_language(code)
    if resolved == DEFAULT_LANGUAGE:
        return key in _resource(DEFAULT_LANGUAGE)
    return key in _resource(resolved)


# ---------------------------------------------------------------------------
# capability - what each language can do here, today
# ---------------------------------------------------------------------------

def _voice_codes() -> dict:
    # Imported lazily so this module stays importable in a context where the
    # speech module is not wanted, and so there is exactly one place where the
    # speech provider's language list lives.
    from .speech import TRANSCRIBE_LANGUAGES
    return TRANSCRIBE_LANGUAGES


def capabilities(code: str) -> dict:
    """What is genuinely available in this language. Derived, not declared.

    `voice` is true only where the speech provider ShopFlow is configured with
    accepts that language - it is read from the provider's own language map,
    not from a wish. `ui` is true where a resource file exists at all, and
    `coverage` says how complete it is. Nothing here is a quality claim.
    """
    resolved = normalize_language(code)
    resource = _resource(resolved)
    return {
        "text": True,          # every supported language accepts typed input
        "ui": bool(resource) or resolved == DEFAULT_LANGUAGE,
        "voice": resolved in _voice_codes(),
        "voiceCode": _voice_codes().get(resolved),
        "whatsapp": True,      # a message is always produced, English if need be
        "coverage": coverage(resolved),
        "confidence": resource_meta(resolved).get("confidence", HIGH),
        "reviewed": False,     # no language here has had native-speaker review
    }


def capability_matrix() -> dict:
    """The whole matrix, one entry per supported language."""
    return {code: capabilities(code) for code in SUPPORTED_LANGUAGES}


def voice_languages() -> Tuple[str, ...]:
    """The languages the configured speech provider accepts. Verified, not assumed."""
    return tuple(c for c in SUPPORTED_LANGUAGES if capabilities(c)["voice"])


# ---------------------------------------------------------------------------
# canonical input
# ---------------------------------------------------------------------------

# Words that carry "I want / give me / how much" in each language. Removing
# them leaves the product description for the EXISTING matcher, which stays
# authoritative. Nothing here maps anything to a SKU, and no brand or product
# noun appears in any of these lists - that vocabulary is read from the
# catalogue at call time by `engine.voice.extract_attributes`.
_FUNCTION_WORDS: Dict[str, Tuple[str, ...]] = {
    "as": ("মোক", "লাগে", "লাগিব", "দিয়ক", "কিমান"),
    "bn": ("আমার", "আমাকে", "চাই", "দরকার", "দিন", "কত"),
    "brx": ("आंनो", "नांगौ", "गोनां", "बेसेन"),
    "doi": ("मिगी", "चाहिदा", "लोड़ींदा", "देओ", "कितना"),
    "gu": ("મને", "જોઈએ", "આપો", "કેટલા", "કેટલું"),
    "hi": ("मुझे", "चाहिए", "दीजिए", "दो", "कितना", "कितने"),
    "kn": ("ನನಗೆ", "ಬೇಕು", "ಕೊಡಿ", "ಎಷ್ಟು"),
    "ks": ("مے", "چھُ", "ضرورت", "دِیُت", "کۅتاہ"),
    "kok": ("माका", "जाय", "दिया", "किदें"),
    "mai": ("हमरा", "चाही", "देल", "केतेक"),
    "ml": ("എനിക്ക്", "വേണം", "തരൂ", "എത്ര"),
    "mni": ("ঐঙোন্দা", "চাই", "পীবিয়ু", "কয়া"),
    "mr": ("मला", "हवेत", "हवे", "हवं", "पाहिजे", "द्या", "किती"),
    "ne": ("मलाई", "चाहियो", "चाहिन्छ", "दिनुहोस्", "कति"),
    "or": ("ମୋତେ", "ଦରକାର", "ଦିଅନ୍ତୁ", "କେତେ"),
    "pa": ("ਮੈਨੂੰ", "ਚਾਹੀਦਾ", "ਚਾਹੀਦੇ", "ਦਿਓ", "ਕਿੰਨਾ"),
    "sa": ("मह्यम्", "आवश्यकम्", "ददातु", "कियत्"),
    "sat": ("ᱤᱧ", "ᱞᱟᱹᱠᱛᱤ", "ᱮᱢᱟᱹᱢ", "ᱛᱤᱱᱟᱹᱜ"),
    "sd": ("مون", "کي", "گهرجي", "ڏيو", "ڪيترو"),
    "ta": ("எனக்கு", "வேண்டும்", "venum", "vendum", "kudunga", "kodunga",
           "anna", "evlo", "evvalavu"),
    "te": ("నాకు", "కావాలి", "ఇవ్వండి", "ఎంత", "kavali"),
    "ur": ("مجھے", "چاہیے", "دیں", "کتنا"),
    "en": ("i", "need", "want", "give", "me", "please", "how", "much", "many"),
}

# Romanised function words for the code-mixed input that Indian retail actually
# uses. Deliberately short and representative - no claim is made that this
# covers Tanglish, Hinglish or any other mixed register in general.
_ROMAN_FUNCTION_WORDS: Dict[str, Tuple[str, ...]] = {
    "ta": ("venum", "vendum", "kudunga", "kodunga", "anna", "evlo"),
    "hi": ("chahiye", "chaahiye", "dijiye", "kitna", "mujhe"),
    "te": ("kavali", "kaavali", "ivvandi", "enta", "naaku"),
    "kn": ("beku", "beeku", "kodi", "eshtu", "nanage"),
    "ml": ("venam", "tharu", "ethra", "enikku"),
    "mr": ("pahije", "hawe", "dya", "kiti", "mala"),
    "bn": ("chai", "lagbe", "din", "koto"),
    "gu": ("joie", "aapo", "ketla"),
    "pa": ("chahida", "chahide", "deo", "kinna"),
}


def normalize_digits(text: str) -> str:
    """Native-script numerals to Latin ones, and nothing else.

    "२० Anchor switch" and "20 Anchor switch" are the same request, so they
    must reach the engine as the same string. This transliterates a numeral;
    it does not read a quantity, and the engine parses and validates the
    number afterwards exactly as it always has.
    """
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in str(text or ""))


def canonicalize_request(text: str, code: str = DEFAULT_LANGUAGE) -> dict:
    """A request in any supported language, reduced to canonical input.

    This is the whole adapter, and it is deliberately small. It normalises
    numerals and removes the language's own function words - "I want", "give
    me", "how much" - so what is left is the product description the EXISTING
    matcher has always received.

    What it does NOT do is the important part. It does not translate a product
    noun, map a phrase to a SKU, choose a unit, or resolve an ambiguity. Brand
    and product vocabulary is passed through exactly as written, because
    "Anchor", "Finolex" and "Havells" are the same words in every one of these
    languages and the catalogue is the only thing entitled to interpret them.
    """
    resolved = normalize_language(code)
    original = str(text or "")
    working = normalize_digits(original)

    removed = []
    words = list(_FUNCTION_WORDS.get(resolved, ()))
    words += list(_ROMAN_FUNCTION_WORDS.get(resolved, ()))
    for word in sorted(set(words), key=len, reverse=True):
        if not word:
            continue
        if word.isascii():
            pattern = re.compile(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])",
                                 re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(word))
        if pattern.search(working):
            working = pattern.sub(" ", working)
            removed.append(word)

    working = re.sub(r"\s+", " ", working).strip()
    canonical = working or normalize_digits(original).strip()

    # Does anything readable by the catalogue survive?
    #
    # This guard exists because of a real failure. The catalogue is written in
    # English, so a request whose product noun is in another script - "20
    # ఆంకర్ స్విచ్" - reduces to the digits alone by the time the matcher
    # tokenises it. "20" then matched a SKU whose code ENDS in 20, exactly,
    # and a customer asking for twenty switches was about to be quoted for
    # 20mm conduit.
    #
    # A bare quantity is not a product description. When nothing but numerals
    # is left, this says so, and the caller asks the owner instead of letting
    # the matcher find something that merely scores well.
    has_vocabulary = bool(re.search(r"[A-Za-z]{2,}", canonical))

    return {
        "language": resolved,
        "originalText": original,
        "canonicalText": canonical,
        "functionWordsRemoved": removed,
        # False when the request carries a quantity but no product wording the
        # catalogue could match. Not an error - a reason to ask.
        "hasProductVocabulary": has_vocabulary,
        # Named so no caller can mistake this for an extraction result. The
        # matcher, not this function, decides what product is meant.
        "resolvesProducts": False,
    }


# ---------------------------------------------------------------------------
# localizing a business result
# ---------------------------------------------------------------------------
# Every function below is ADDITIVE. It reads a structured result the engine
# produced and returns a block of labels and pre-formatted strings to display
# beside it. None of them mutates the result, and none of them recomputes a
# figure: a rupee value in a localized block is `format_rupees` applied to the
# engine's own number and nothing else.

_CREDIT_KEYS = {
    "APPROVED": "credit.approved",
    "LIMIT_EXCEEDED": "credit.limitExceeded",
    "BLOCKED": "credit.blocked",
    "NO_CREDIT_ACCOUNT": "credit.noAccount",
}

_MARGIN_KEYS = {
    "HEALTHY": "margin.healthy",
    "MARGIN_REDUCED": "margin.reduced",
    "LOW_MARGIN": "margin.low",
    "NEGATIVE_MARGIN": "margin.negative",
    "UNAVAILABLE": "margin.unavailable",
}

_CLARIFY_KEYS = {
    "colour": "clarify.colour",
    "color": "clarify.colour",
    "specification": "clarify.specification",
    "brand": "clarify.brand",
    "uom": "clarify.unit",
    "unit": "clarify.unit",
}


def _money(value) -> Optional[str]:
    """The engine's own number, grouped for reading. No arithmetic."""
    return None if value is None else format_rupees(value)


def localize_quote(quote: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """Labels for a quotation, and its total as text. The total is not touched."""
    if not isinstance(quote, dict):
        return {}
    return {
        "language": normalize_language(code),
        "direction": direction(code),
        "heading": translate(code, "quote.heading"),
        "itemsLabel": translate(code, "quote.items"),
        "customerLabel": translate(code, "quote.customer"),
        "totalLabel": translate(code, "quote.total"),
        "totalText": _money(quote.get("total")),
        "notice": translate(code, "quote.notInvoice"),
    }


def localize_credit_status(credit: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """Labels for a khata decision. The decision itself is not re-made here."""
    if not isinstance(credit, dict) or not credit.get("decision"):
        return {}
    decision = credit["decision"]
    return {
        "language": normalize_language(code),
        "direction": direction(code),
        "heading": translate(code, "credit.heading"),
        "statusLabel": translate(code, "credit.status"),
        # The engine's decision, worded. The code itself is carried through
        # unchanged so nothing downstream has to read a translated string.
        "decision": decision,
        "decisionText": translate(code, _CREDIT_KEYS.get(decision, "")) or decision,
        "limitLabel": translate(code, "credit.limit"),
        "limitText": _money(credit.get("creditLimit")),
        "outstandingLabel": translate(code, "credit.outstanding"),
        "outstandingText": _money(credit.get("currentOutstanding")),
        "projectedLabel": translate(code, "credit.projected"),
        "projectedText": _money(credit.get("projectedOutstanding")),
        "remainingLabel": translate(code, "credit.remaining"),
        "remainingText": _money(credit.get("remainingCredit")),
        # Carried in every language, because it is a statement about what
        # ShopFlow is, not a piece of interface text.
        "policy": credit.get("policy"),
    }


def localize_margin_status(margin: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """Labels for a margin view. Shop-internal - this never goes to a customer."""
    if not isinstance(margin, dict) or not margin.get("status"):
        return {}
    status = margin["status"]
    return {
        "language": normalize_language(code),
        "direction": direction(code),
        "status": status,
        "statusText": translate(code, _MARGIN_KEYS.get(status, "")) or status,
        "previousLabel": translate(code, "margin.previous"),
        "previousText": _money(margin.get("oldMarginAmount")),
        "currentLabel": translate(code, "margin.current"),
        "currentText": _money(margin.get("newMarginAmount")),
        "reductionLabel": translate(code, "margin.reduction"),
        "reductionText": _money(margin.get("marginReductionAmount")),
    }


def localize_purchase_plan(plan: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """Labels for a purchase plan. The allocator is not consulted or altered."""
    if not isinstance(plan, dict):
        return {}
    return {
        "language": normalize_language(code),
        "direction": direction(code),
        "heading": translate(code, "plan.heading"),
        "budgetLabel": translate(code, "plan.budget"),
        "budgetText": _money(plan.get("budget")),
        "allocatedLabel": translate(code, "plan.allocated"),
        "allocatedText": _money(plan.get("totalSpend")),
        "leftOverLabel": translate(code, "plan.leftOver"),
        "leftOverText": _money(plan.get("remaining")),
    }


def localize_clarification(clarification: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """The question, in the owner's language. The DECISION to ask is the engine's.

    The engine decided NEEDS_CLARIFICATION and chose the candidates. This puts
    that question into words, and cannot pick, narrow or reorder an option -
    the candidate list is passed through untouched.
    """
    if not isinstance(clarification, dict):
        return {}
    # `clarifyingAttribute` is the name the order flow actually uses; reading
    # only the other two left every order clarification on the generic
    # "please choose one". The orchestrator now sets this attribute only when
    # the real options differ on it, so the localized question is grounded.
    asked = str(clarification.get("attribute") or clarification.get("missing")
                or clarification.get("clarifyingAttribute") or "")
    key = _CLARIFY_KEYS.get(asked.lower())
    question = translate(code, key) if key else ""
    return {
        "language": normalize_language(code),
        "direction": direction(code),
        "heading": translate(code, "clarify.heading"),
        # A recognised attribute gets its own question; anything else gets the
        # generic one, never a guessed translation of the engine's wording.
        "question": question or translate(code, "clarify.pickOne"),
        "chooseLabel": translate(code, "clarify.pickOne"),
        "attribute": asked,
    }


def localize_error(reason: str, code: str = DEFAULT_LANGUAGE) -> str:
    """A user-facing error. Diagnostics are never translated, or shown."""
    keys = {
        "NOT_FOUND": "error.notFound",
        "NO_SPEECH": "error.noSpeech",
    }
    return translate(code, keys.get(str(reason or "").upper(), "error.generic"))


def localize_response(payload: dict, code: str = DEFAULT_LANGUAGE) -> dict:
    """Attach a localization block to a business response, changing nothing else.

    A copy is returned with one extra key, `localized`. Every business field
    in the original - totals, SKUs, quantities, units, decisions - is carried
    through by reference and untouched, which is what makes the equivalence
    tests able to compare a localized response with an English one field by
    field.
    """
    if not isinstance(payload, dict):
        return payload
    resolved = normalize_language(code)
    block = {
        "language": resolved,
        "direction": direction(resolved),
        "nativeName": _REGISTRY[resolved]["nativeName"],
        "coverage": coverage(resolved),
        "reviewNotice": REVIEW_NOTICE,
    }
    if payload.get("quote"):
        block["quote"] = localize_quote(payload["quote"], resolved)
    if payload.get("credit"):
        block["credit"] = localize_credit_status(payload["credit"], resolved)
    if payload.get("clarification"):
        block["clarification"] = localize_clarification(
            payload["clarification"], resolved)
    if payload.get("plan"):
        block["plan"] = localize_purchase_plan(payload["plan"], resolved)
    return {**payload, "localized": block}
