"""Voice as an interface over the existing workflows - not a second brain.

WHAT THIS MODULE IS
-------------------
A shop owner with both hands on a wire coil cannot type. This module turns
what they said into the same structured inputs the typed flow already uses,
and turns the engine's answer back into one short spoken sentence.

It does four things, all of them pure:

  normalize_transcript   fix the predictable damage speech-to-text does to
                         electrical-shop vocabulary
  detect_language        Tamil, English or mixed, so the reply matches
  classify_intent        which existing workflow the owner is asking for
  answer_shop_query      look up stock/price/availability via the EXISTING
                         matcher and format one spoken line

WHAT IT IS NOT
--------------
It is not a language model, it is not a chatbot, and it invents nothing. Every
number in a spoken sentence is interpolated from a value the deterministic
engine returned. There is no arithmetic in this file beyond formatting, and no
product knowledge that is not read from the catalogue at call time.

The matcher stays authoritative. Normalisation only tidies wording before
`resolve_product` sees it - it never maps a phrase to a SKU, and it never
resolves an ambiguity. If several real products still fit, the owner is asked,
exactly as in the typed flow.

TAMIL AND TANGLISH
------------------
Shop speech in Madurai is mixed: English product nouns, Tamil grammar around
them ("20 Anchor switch venum"). Rather than attempt Tamil NLP, this keeps a
small controlled alias table for the terms that actually occur in this trade,
and leaves everything else alone. That is a deliberate MVP boundary, not an
approximation of a general translator.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from .margin import margin_view
from .messages import format_rupees
from .matching import (AMBIGUOUS, NOT_FOUND, RESOLVED, normalize_ratings,
                       resolve_product)
from .models import Dataset, money

# --- languages --------------------------------------------------------------
TAMIL = "ta"
ENGLISH = "en"
MIXED = "mixed"

# --- intents ----------------------------------------------------------------
INTENT_ORDER = "ORDER"
INTENT_STOCK = "STOCK"
INTENT_PRICE = "PRICE"
INTENT_AVAILABILITY = "AVAILABILITY"
INTENT_MARGIN = "MARGIN"
INTENT_QUOTE_TOTAL = "QUOTE_TOTAL"
INTENT_PURCHASE_PLAN = "PURCHASE_PLAN"
INTENT_CONFIRM_PRICE = "CONFIRM_SUPPLIER_PRICE"
INTENT_UNKNOWN = "UNKNOWN"

# Intents this module answers itself, using the existing matcher.
LOOKUP_INTENTS = (INTENT_STOCK, INTENT_PRICE, INTENT_AVAILABILITY, INTENT_MARGIN)

# Intents that are handed back to an existing workflow rather than answered
# here. Voice must not grow a parallel implementation of either one.
DELEGATED_INTENTS = (INTENT_ORDER, INTENT_PURCHASE_PLAN, INTENT_QUOTE_TOTAL)

MAX_TRANSCRIPT_CHARS = 1000

# Tamil Unicode block. Presence of any of these is decisive.
_TAMIL_RE = re.compile(r"[஀-௿]")

# Romanised Tamil function words that show up constantly in shop speech. These
# carry the grammar, so they are what distinguish Tanglish from English.
_TANGLISH_MARKERS = frozenset("""
anna venum vendum irukku irukka irukkuma evlo evvalavu enna eppadi
la le ku ki ah aa oru rendu moonu naalu anju pannunga panna mudiyum
kudunga kodunga vanthu konjam romba illa illai seri sari nalla
podunga poduga vaangu vaanga kaasu paisa mattum ellam
""".split())

# Speech-to-text mangles trade vocabulary in predictable ways. Only terms that
# actually exist in this catalogue are corrected - nothing here invents a
# product, and anything unrecognised is passed through untouched.
_ALIASES: Dict[str, str] = {
    # units of measure
    "amps": "amp", "ampere": "amp", "amperes": "amp", "am": "amp",
    "square mm": "sq mm", "squaremm": "sq mm", "sqmm": "sq mm",
    "square millimeter": "sq mm", "square millimetre": "sq mm",
    "sq.mm": "sq mm", "mm2": "sq mm",
    "metre": "m", "meter": "m", "metres": "m", "meters": "m",
    "coils": "coil", "bundle": "coil", "bundles": "coil",
    "rolls": "coil", "kandu": "coil",
    "pieces": "piece", "pcs": "piece", "nos": "piece", "numbers": "piece",
    "packs": "pack", "packet": "pack", "packets": "pack",
    "boxes": "box", "petti": "box", "cartons": "box", "carton": "box",
    "lengths": "length", "rods": "length", "rod": "length",
    # categories as spoken
    "switches": "switch", "swich": "switch", "swith": "switch",
    "sockets": "socket", "plug point": "socket", "plug points": "socket",
    "wires": "wire", "cables": "cable", "cabel": "cable",
    "mcbs": "mcb", "m c b": "mcb", "miniature circuit breaker": "mcb",
    "single pole": "SP", "double pole": "DP",
    "fans": "fan", "ceiling fans": "fan",
    "bulbs": "bulb", "lamps": "lamp", "led lamp": "LED", "leds": "LED",
    "tube light": "LED", "holders": "holder",
    "conduits": "conduit", "p v c": "PVC", "boxes": "box",
    # brand mishearings seen in this catalogue's vocabulary
    "fino lex": "Finolex", "finolux": "Finolex", "phinolex": "Finolex",
    "anchar": "Anchor", "ankur": "Anchor", "anker": "Anchor",
    "havels": "Havells", "havell": "Havells", "haveli": "Havells",
    "schnieder": "Schneider", "schnider": "Schneider",
    "legrande": "Legrand", "ligrand": "Legrand",
    "poly cab": "Polycab", "polycap": "Polycab",
    "crompton greaves": "Crompton", "orent": "Orient",
    "philips": "Philips", "filips": "Philips", "syskaa": "Syska",
    "g m modular": "GM Modular", "gm": "GM Modular",
    # Tamil colour words -> the catalogue's own colour values
    "sivappu": "Red", "chivappu": "Red",
    "neelam": "Blue", "neela": "Blue",
    "karuppu": "Black", "karuppu": "Black",
    "vellai": "White", "vella": "White",
    "pazhuppu": "Brown",
    # Tamil trade nouns -> catalogue vocabulary
    "kambi": "wire", "waiyar": "wire",
    "suvitch": "switch", "svich": "switch",
    "vilai": "price", "rate": "price",
    "stocku": "stock", "sarakku": "stock",
}

# Longest-first so "square mm" wins over "mm".
_ALIAS_KEYS = sorted(_ALIASES, key=len, reverse=True)

_STOCK_WORDS = ("stock", "stocku", "sarakku", "inventory", "how many", "balance")
_PRICE_WORDS = ("price", "vilai", "rate", "cost", "how much is", "mrp")
_AVAIL_WORDS = ("irukka", "irukkuma", "available", "availability",
                "do you have", "do we have", "got any", "in stock")
_MARGIN_WORDS = ("margin", "laabam", "labam", "laabham", "profit")
_TOTAL_WORDS = ("total", "mottham", "grand total", "bill")
_PLAN_WORDS = ("budget", "purchase plan", "purchase panna", "vaanga",
               "what can i buy", "plan panna")
_CONFIRM_WORDS = ("confirm", "approve", "accept the price", "ok panna")
_ORDER_WORDS = ("venum", "vendum", "need", "want", "give me", "order",
                "kudunga", "kodunga", "send", "bill panna")

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def normalize_transcript(text: str) -> Tuple[str, List[str]]:
    """Tidy a raw transcript, and report every substitution made.

    Returns the cleaned text and the list of aliases applied, so the interface
    can show the owner exactly what was changed. Silent rewriting of someone's
    words is not acceptable in a system that then quotes them a price.

    This only ever rewrites wording. It never decides which product is meant.
    """
    raw = unicodedata.normalize("NFC", str(text or ""))[:MAX_TRANSCRIPT_CHARS]
    # Speech engines punctuate erratically; commas and full stops carry no
    # meaning here, but hyphens inside "1-Way" do - and a full stop BETWEEN
    # DIGITS is a decimal point, not punctuation. Stripping it turned
    # "1.5 sq mm" into "1 5" and the wire gauge was lost, which is how this
    # exception came to be written.
    cleaned = re.sub(r"(?<!\d)[,.!?;:]+(?!\d)", " ", raw)
    cleaned = re.sub(r"[,!?;:]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    applied: List[str] = []
    lowered = cleaned.lower()

    for key in _ALIAS_KEYS:
        if key not in lowered:
            continue
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])",
                             re.IGNORECASE)
        if pattern.search(cleaned):
            cleaned = pattern.sub(_ALIASES[key], cleaned)
            lowered = cleaned.lower()
            applied.append(f"{key} -> {_ALIASES[key]}")

    # A spoken current rating is written the catalogue's way: "32 amp" and
    # "10amp" become "32A" and "10A", and the owner is shown the change. It
    # used to be split into "32 amp", which the matcher read as a count and
    # an article, so the rating was lost.
    cleaned, ratings = normalize_ratings(cleaned)
    applied.extend(ratings)
    # "1.5sqmm" is common; split it so the matcher tokenises.
    cleaned = re.sub(r"(\d)\s*(sq mm|mm|w|v)\b", r"\1 \2", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, applied


# Brand names Amazon Transcribe produced on the live deployment that are too
# far from the brand for the edit-distance rule below: "2 Hels MCB SP 32 amp"
# was Havells (2026-09-27). Each is used only with the context rule too.
_OBSERVED_BRAND_FORMS: Dict[str, str] = {"hels": "Havells"}
_MAX_BRAND_EDITS = 2
_MIN_FUZZY_LENGTH = 5
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
# Where one spoken order line ends: the context rule reads one line at a time.
_LINE_BREAK = re.compile(r"[,;\n]|\band\b|\balso\b|\bplus\b", re.IGNORECASE)


def _edits(a: str, b: str) -> int:
    """Optimal string alignment distance (Damerau-Levenshtein, restricted)."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1,
                             rows[i - 1][j - 1] + cost)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def brand_vocabulary(data: Dataset) -> dict:
    """What the catalogue says about its brands: each single-word brand, the
    words its own products are described with, and every catalogue word."""
    from .matching import _product_tokens

    brands: Dict[str, set] = {}
    known: set = set()
    for product in data.products.values():
        tokens = set(_product_tokens(product))
        known |= tokens
        if " " in product.brand.strip():
            continue      # "RR Kabel", "GM Modular": exact names only
        words = {t for t in tokens if t.isalpha() and len(t) >= 3}
        words.discard(product.brand.lower())
        brands.setdefault(product.brand, set()).update(words)
    return {"brands": brands, "known": known}


def correct_brand_terms(text: str, vocabulary: dict) -> Tuple[str, List[str]]:
    """Correct a misheard brand name - only when it can only be one brand.

    A spoken word becomes a catalogue brand when all three hold:

      1. it is not already a word the catalogue uses ("anchor", "wire", ...);
      2. it is within two edits of exactly ONE catalogue brand (and is at
         least five letters long), or it is a form Transcribe was observed to
         produce for that brand;
      3. the same order line names something that brand actually sells -
         "hevels MCB" can be Havells, "hevels" alone is left as heard.

    Anything else is left exactly as heard, and the matcher asks. Every
    correction is returned so the owner sees it ("hevels -> Havells").
    """
    brands, known = vocabulary["brands"], vocabulary["known"]
    applied: List[str] = []
    out, last = [], 0
    for match in _WORD_RE.finditer(text):
        word = match.group().replace("'", "")
        low = word.lower()
        if low in known or any(low == b.lower() for b in brands):
            continue
        candidates = {b for b in brands
                      if _OBSERVED_BRAND_FORMS.get(low) == b
                      or (len(low) >= _MIN_FUZZY_LENGTH
                          and _edits(low, b.lower()) <= _MAX_BRAND_EDITS)}
        if len(candidates) != 1:
            continue                          # none, or ambiguous: ask
        brand = candidates.pop()
        start = max((m.end() for m in _LINE_BREAK.finditer(text, 0, match.start())),
                    default=0)
        stop = _LINE_BREAK.search(text, match.end())
        line = text[start:stop.start() if stop else len(text)].lower()
        line_words = set(re.findall(r"[a-z]+", line))
        if not (line_words & brands[brand]):
            continue                          # nothing this brand sells named
        out.append(text[last:match.start()])
        out.append(brand)
        last = match.end()
        applied.append(f"{match.group()} -> {brand}")
    out.append(text[last:])
    return "".join(out), applied


def detect_language(text: str) -> str:
    """Tamil, English, or mixed - decided from script and function words.

    Deliberately shallow. The only decision this drives is which language the
    reply is spoken in, so a wrong answer is a mildly odd sentence, never a
    wrong number.
    """
    raw = str(text or "")
    has_tamil_script = bool(_TAMIL_RE.search(raw))

    words = [w.strip(".,!?;:").lower() for w in raw.split()]
    tanglish = sum(1 for w in words if w in _TANGLISH_MARKERS)
    latin = sum(1 for w in words if re.search(r"[a-z]", w))

    if has_tamil_script and latin:
        return MIXED
    if has_tamil_script:
        return TAMIL
    if tanglish:
        # Romanised Tamil grammar around English product nouns.
        return MIXED if latin > tanglish else TAMIL
    return ENGLISH


def speaks_tamil(language: str) -> bool:
    """Whether the reply should carry Tamil. English input never forces it."""
    return language in (TAMIL, MIXED)


# ---------------------------------------------------------------------------
# intent
# ---------------------------------------------------------------------------

def _contains(text: str, words) -> bool:
    return any(w in text for w in words)


def extract_budget(text: str) -> Optional[float]:
    """The largest plain number in the sentence, read as rupees.

    Only used for a purchase-plan request, where the number IS the budget. The
    planner validates it again, so a misread here cannot produce a bad plan -
    only a plan for the wrong amount, which the owner sees on screen.
    """
    found = _NUMBER_RE.findall(text or "")
    values = []
    for raw in found:
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


def classify_intent(text: str) -> dict:
    """Which existing workflow is being asked for. Keyword rules, no model.

    Order matters. "Indha order total evlo?" contains both a total word and a
    price word, and it is a total question; "5000 budget la enna vaanga
    mudiyum?" contains a number and a product-ish word but is a planning
    question. The more specific markers are therefore tested first.
    """
    lowered = (text or "").lower()

    if not lowered.strip():
        return {"intent": INTENT_UNKNOWN, "subject": "", "budget": None}

    def result(intent, **extra):
        out = {"intent": intent, "subject": _subject(lowered, intent),
               "budget": None}
        out.update(extra)
        return out

    # Consequential. Never executed here - see answer_shop_query.
    if _contains(lowered, _CONFIRM_WORDS) and (
            "price" in lowered or "supplier" in lowered or "cost" in lowered):
        return result(INTENT_CONFIRM_PRICE)

    # Before every other lookup word. "Indha wire-la margin evlo?" contains a
    # price word too, and it is a margin question.
    if _contains(lowered, _MARGIN_WORDS):
        return result(INTENT_MARGIN)

    if _contains(lowered, _PLAN_WORDS):
        return result(INTENT_PURCHASE_PLAN, budget=extract_budget(lowered))

    if _contains(lowered, _TOTAL_WORDS):
        return result(INTENT_QUOTE_TOTAL)

    if _contains(lowered, _STOCK_WORDS):
        return result(INTENT_STOCK)

    if _contains(lowered, _PRICE_WORDS):
        return result(INTENT_PRICE)

    if _contains(lowered, _AVAIL_WORDS):
        return result(INTENT_AVAILABILITY)

    if _contains(lowered, _ORDER_WORDS) or _NUMBER_RE.search(lowered):
        # A quantity with no question word is an order being placed.
        return result(INTENT_ORDER)

    return result(INTENT_UNKNOWN)


# Words that describe the question rather than the product. Stripping them
# leaves the matcher with just the product description.
_NOISE = frozenset("""
stock stocku sarakku inventory balance price vilai rate cost mrp
margin laabam labam laabham profit indha idhu this that
irukka irukkuma irukku available availability in of the a an is are
how many much do you have we got any what enna evlo evvalavu la le ku
anna please tell me show na oru total mottham bill and
""".split())


def _subject(lowered: str, intent: str) -> str:
    """The product part of a lookup question, with question words removed."""
    if intent not in LOOKUP_INTENTS:
        return ""
    words = [w.strip("?.,!") for w in lowered.split()]
    kept = [w for w in words if w and w not in _NOISE]
    return " ".join(kept).strip()



# ---------------------------------------------------------------------------
# attribute extraction - read from the catalogue, never invented
# ---------------------------------------------------------------------------

# Spoken category words mapped to the catalogue's own category values. Only
# categories that exist in the dataset appear here, and a word is mapped only
# where the trade meaning is unambiguous. Anything doubtful is left out, so the
# matcher falls back to free-text ranking rather than being wrongly narrowed.
_CATEGORY_WORDS = {
    "switch": "Switch", "socket": "Switch", "modular": "Switch",
    "wire": "Wire", "cable": "Wire", "coil": "Wire",
    "mcb": "MCB", "breaker": "MCB",
    "fan": "Ceiling Fan",
    "led": "LED Lamp", "lamp": "LED Lamp", "bulb": "LED Lamp",
}

# A bare integer in shop speech is a quantity ("2 Havells MCB"), not a
# specification. A number is only read as a specification when it carries a
# unit, or is a decimal like "1.5", or is a pole pattern like "1-way".
_SPEC_WITH_UNIT = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(amp|a|sq mm|sqmm|mm|w|v)\b", re.IGNORECASE)
_SPEC_DECIMAL = re.compile(r"\b(\d+\.\d+)\b")
_SPEC_WAY = re.compile(r"\b(\d+\s*-?\s*way)\b", re.IGNORECASE)
_SPEC_POLE = re.compile(r"\b(sp|dp)\b", re.IGNORECASE)


def _catalogue_values(data: Dataset, attr: str):
    """Distinct non-empty values of one product attribute, longest first.

    Longest first so "Cool White" is matched before "White", and
    "GM Modular" before "Modular".
    """
    values = {getattr(p, attr) for p in data.products.values()}
    return sorted((v for v in values if v), key=len, reverse=True)


def extract_attributes(data: Dataset, text: str) -> dict:
    """Pull stated product attributes out of a phrase, using the catalogue.

    Every brand, colour and length recognised here is one that actually exists
    in this shop's catalogue - the vocabulary is read from the data at call
    time, not hard-coded. Nothing is guessed: an attribute the owner did not
    say is left unset, and an unset attribute is not a filter.

    This narrows the search. It never chooses a SKU - `resolve_product` still
    decides, and still asks when more than one product fits.
    """
    lowered = f" {(text or '').lower()} "
    found = {}

    for attr in ("brand", "colour", "length"):
        for value in _catalogue_values(data, attr):
            if f" {value.lower()} " in lowered or f" {value.lower()}," in lowered:
                found[attr] = value
                break

    for word, category in _CATEGORY_WORDS.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", lowered):
            found["category"] = category
            break

    spec_parts = []
    for match in _SPEC_WITH_UNIT.finditer(lowered):
        spec_parts.append(match.group(1))
    for pattern in (_SPEC_DECIMAL, _SPEC_WAY, _SPEC_POLE):
        for match in pattern.finditer(lowered):
            token = match.group(1).strip()
            if token not in spec_parts:
                spec_parts.append(token)
    if spec_parts:
        # De-duplicated, order preserved: "1-way 10" reads as one specification.
        seen, ordered = set(), []
        for part in spec_parts:
            if part not in seen:
                seen.add(part)
                ordered.append(part)
        found["specification"] = " ".join(ordered)

    return found


# ---------------------------------------------------------------------------
# spoken answers - every figure comes from the engine
# ---------------------------------------------------------------------------

def _rupees(value: float) -> str:
    """Indian digit grouping, for a figure the engine produced.

    Delegates to `engine.messages.format_rupees` so a rupee amount reads
    identically whether it is spoken aloud, shown on screen or sent to a
    customer. One implementation, three callers.
    """
    return format_rupees(money(value))


def speak_stock(product, on_hand: int, tamil: bool) -> str:
    unit = product.unit + ("s" if on_hand != 1 else "")
    if tamil:
        if on_hand == 0:
            return f"{product.name} stock-la illai."
        return f"{product.name}. Stock-la {on_hand} {unit} irukku."
    if on_hand == 0:
        return f"{product.name} is out of stock."
    return f"{product.name}. {on_hand} {unit} in stock."


def speak_price(product, tamil: bool) -> str:
    price = _rupees(product.sellingPrice)
    if tamil:
        return f"{product.name} price {price} per {product.unit}."
    return f"{product.name} is {price} per {product.unit}."


def speak_availability(product, on_hand: int, tamil: bool) -> str:
    if tamil:
        if on_hand > 0:
            return f"Aamaam, {product.name} {on_hand} {product.unit} irukku."
        return f"Illai, {product.name} ippo stock-la illai."
    if on_hand > 0:
        return f"Yes, {on_hand} {product.unit} of {product.name} in stock."
    return f"No, {product.name} is not in stock right now."


def speak_margin(view: dict, tamil: bool) -> str:
    """Read back a margin the deterministic engine already calculated.

    Every figure is interpolated straight out of `engine.margin.margin_view`.
    There is no subtraction, no percentage and no threshold test in this
    function - if the engine did not produce a number, this cannot say one.
    """
    if not view.get("available"):
        if tamil:
            return "Indha product-ku margin data illai."
        return "There is not enough recorded cost data to show a margin."

    name = view.get("productName") or ""
    unit = view.get("unit") or "unit"

    if not view.get("comparisonAvailable"):
        current = _rupees(view["oldMarginAmount"])
        if tamil:
            return (f"{name} margin {current} per {unit}. "
                    f"Supplier price change edhuvum confirm aagala.")
        return (f"{name} margin is {current} per {unit}. "
                f"No supplier price change has been confirmed.")

    new = _rupees(view["newMarginAmount"])
    old = _rupees(view["oldMarginAmount"])
    cut = _rupees(view["marginReductionAmount"])
    if tamil:
        return (f"Ippo margin {new}. Munnadi {old}. "
                f"{cut} margin kammi aagirukku.")
    return f"Current margin {new}. Previous margin {old}. Margin reduced by {cut}."


def speak_clarification(match, tamil: bool) -> str:
    """Ask, exactly as the typed flow does. Never pick a variant."""
    attr = match.clarifyingAttribute or "variant"
    if tamil:
        return f"Ethu venum? {attr} confirm pannunga."
    return f"Several products match. Which {attr} do you need?"


def speak_not_found(requested: str, tamil: bool) -> str:
    if tamil:
        return f"'{requested}' kadai catalogue-la kaanom. Vera peru sollunga."
    return f"No catalogue product matches '{requested}'."


def speak_quote(quote: dict, tamil: bool) -> str:
    """One sentence from a quote the engine already calculated."""
    total = _rupees(quote.get("total", 0))
    shorts = [l for l in quote.get("lines", []) if l.get("shortageQty", 0) > 0]
    if tamil:
        line = f"Total {total}."
        if shorts:
            parts = [f"{l['name']} {l['shortageQty']} {l.get('unit','')} shortage"
                     for l in shorts]
            line += " " + ", ".join(parts) + "."
        return line
    line = f"Total {total}."
    if shorts:
        parts = [f"{l['name']} short by {l['shortageQty']}" for l in shorts]
        line += " " + ", ".join(parts) + "."
    return line


def speak_plan(plan: dict, tamil: bool) -> str:
    """One sentence from a plan the deterministic allocator produced."""
    budget = _rupees(plan.get("budget", 0))
    selected = plan.get("counts", {}).get("restockSelected", 0)
    deferred = plan.get("counts", {}).get("restockDeferred", 0)
    remaining = _rupees(plan.get("remaining", 0))
    if tamil:
        return (f"{budget} budget-la {selected} items purchase panna mudiyum. "
                f"{deferred} items defer aagum. {remaining} meethi. "
                f"Details screen-la paarunga.")
    return (f"With {budget}, {selected} restocks are funded and {deferred} are "
            f"deferred. {remaining} left over. Details are on screen.")


# ---------------------------------------------------------------------------
# lookup, through the existing matcher
# ---------------------------------------------------------------------------

def answer_shop_query(
    data: Dataset,
    transcript: str,
    confirmed_costs: Optional[Dict[str, float]] = None,
) -> dict:
    """Answer a spoken stock / price / availability question.

    Product resolution is delegated to `resolve_product` - the same matcher the
    typed order flow uses - so an ambiguous phrase produces the same question
    here as it does there, and an unknown one is refused the same way.

    `confirmed_costs` are the shop's durable confirmed supplier costs, passed
    in by the caller. A margin question is answered by handing one of them to
    `engine.margin.margin_view` and reading the result aloud; this module
    performs none of that arithmetic itself.

    ORDER and PURCHASE_PLAN are deliberately NOT answered here. They are
    reported back so the caller can use the existing endpoints; duplicating
    them would create a second implementation of the business workflow.
    """
    normalized, aliases = normalize_transcript(transcript)
    normalized, brand_fixes = correct_brand_terms(normalized,
                                                  brand_vocabulary(data))
    aliases = aliases + brand_fixes
    language = detect_language(transcript)
    tamil = speaks_tamil(language)
    intent = classify_intent(normalized)

    base = {
        "transcript": str(transcript or "")[:MAX_TRANSCRIPT_CHARS],
        "normalizedTranscript": normalized,
        "language": language,
        "aliasesApplied": aliases,
        "intent": intent["intent"],
        "attributes": {},
        "requiresConfirmation": False,
        "handledBy": "voice",
    }

    name = intent["intent"]

    # A spoken instruction never changes shop state. It can only prepare one.
    if name == INTENT_CONFIRM_PRICE:
        return {
            **base,
            "status": "NEEDS_HUMAN_CONFIRMATION",
            "requiresConfirmation": True,
            "spokenText": (
                "Supplier price confirmation screen-la manually confirm pannunga."
                if tamil else
                "Please confirm the supplier price on screen. Voice cannot "
                "confirm a price change."),
        }

    if name in DELEGATED_INTENTS:
        return {
            **base,
            "status": "DELEGATE",
            "delegateTo": name,
            "budget": intent.get("budget"),
            "handledBy": "existing-workflow",
            "spokenText": "",
        }

    if name == INTENT_UNKNOWN or not intent["subject"]:
        return {
            **base,
            "status": "NOT_UNDERSTOOD",
            "spokenText": ("Purinjikala. Marupadiyum sollunga."
                           if tamil else
                           "Sorry, I did not catch that. Please try again."),
        }

    # Stated attributes become hard filters, exactly as they do for the typed
    # flow. The matcher still owns the decision and still asks when several
    # real products fit.
    attributes = extract_attributes(data, intent["subject"])
    match = resolve_product(data, requested_text=intent["subject"], **attributes)

    if match.status == NOT_FOUND:
        return {
            **base,
            "attributes": attributes,
            "status": NOT_FOUND,
            "match": match.as_dict(),
            "spokenText": speak_not_found(intent["subject"], tamil),
        }

    if match.status == AMBIGUOUS:
        # Same rule as the typed flow: more than one real product fits, so ask.
        return {
            **base,
            "attributes": attributes,
            "status": AMBIGUOUS,
            "match": match.as_dict(),
            "clarifyingAttribute": match.clarifyingAttribute,
            "options": match.options,
            "spokenText": speak_clarification(match, tamil),
        }

    product = data.product(match.skuId)
    on_hand = data.onHand(match.skuId)

    margin = None
    if name == INTENT_MARGIN:
        # The deterministic engine decides what the margin is. This module only
        # chooses which SKU to ask about and how to say the answer.
        margin = margin_view(
            data, match.skuId, (confirmed_costs or {}).get(match.skuId))
        spoken = speak_margin(margin, tamil)
    elif name == INTENT_STOCK:
        spoken = speak_stock(product, on_hand, tamil)
    elif name == INTENT_PRICE:
        spoken = speak_price(product, tamil)
    else:
        spoken = speak_availability(product, on_hand, tamil)

    return {
        **base,
        "attributes": attributes,
        "status": RESOLVED,
        "match": match.as_dict(),
        # Straight from the catalogue and inventory - nothing derived here.
        "product": {
            "skuId": product.skuId,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
            "specification": product.specification,
            "colour": product.colour,
            "unit": product.unit,
            "sellingPrice": product.sellingPrice,
            "onHand": on_hand,
        },
        "spokenText": spoken,
        # Present only for a margin question, and carried through exactly as
        # the engine returned it.
        **({"margin": margin} if margin is not None else {}),
    }
