"""Deterministic product matching.

The language model may describe what a customer asked for. It may not decide
which SKU that is. Resolution happens here, over the real catalogue, by rules
that can be read and tested.

Three outcomes, and only three:

  RESOLVED   exactly one catalogue product fits
  AMBIGUOUS  several fit and they differ on an attribute the customer did not
             give - the caller must ask, never guess
  NOT_FOUND  nothing fits

A tie is never broken automatically. If two real products both fit the words
the customer used, ShopFlow asks - it does not pick the shop's usual variant
and show a badge about it. On a quotation the wrong variant means the wrong
price and the wrong delivery, and a default that is merely disclosed is still a
choice the customer never made.

`Product.isDefaultVariant` remains in the catalogue as metadata for owner-facing
features later. It takes no part in resolving a customer order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .models import Dataset, Product
from .uom import normalize_uom, product_uom

RESOLVED = "RESOLVED"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"

# Attributes a customer may leave unsaid, in the order we would ask about them.
CLARIFIABLE_ATTRS = ("brand", "specification", "colour", "length")

_WORD = re.compile(r"[a-z0-9.]+")

# Spoken forms that map onto catalogue vocabulary.
_SYNONYMS = {
    "sqmm": "sqmm", "sq": "", "mm": "", "square": "", "millimeter": "",
    "millimetre": "", "amp": "a", "amps": "a", "ampere": "a", "amperes": "a",
    "switches": "switch", "coils": "coil", "coil": "coil", "wires": "wire",
    "bulbs": "bulb", "lamps": "lamp", "fans": "fan", "pieces": "piece",
    "modular": "modular", "anna": "", "please": "", "need": "", "want": "",
}

_STOPWORDS = {"", "the", "a", "of", "for", "and", "with", "me", "give", "get"}

# A number and the word it qualifies are one specification, however they are
# written: "1-Way", "1 way" and "1way" are all "1way"; "3 pin" is "3pin".
#
# Split apart, "1-Way" became the tokens "1" and "way", and a bare "2" - the
# customer's COUNT - scored as a hit on "2-Way". An evaluator's order "2 Anchor
# modular switches 1-Way 10A White and 3 coils Finolex..." tied 1-Way with
# 2-Way and asked "1-Way 10A or 2-Way 10A?" three times out of three, although
# the customer had said 1-Way. Joined, a specification carries its number
# inside it and a count cannot touch it.
_COMPOUND = re.compile(
    r"\b(\d+)\s*-?\s*(way|pin|plate|core|module|gang)s?\b")


# An explicit current rating: "32 amp", "32 amps", "32 ampere", "32 A" are the
# catalogue's "32A". Split apart, "32" read as a count and "a" as the article,
# so a spoken "2 Havells MCB 32 amp C curve" lost its rating and was asked
# "which size or rating?" although the customer had said it. Only rating
# language is joined: a bare "32" stays a number, "32 kg" stays a weight. A
# lower-case lone "a" is joined only where it cannot be the article - at the
# end, before punctuation, or before a curve or pole word.
_RATING_FORMS = (
    # "32 amp", "32 amps", "32 ampere", "32 Amperes" - any case
    re.compile(r"(?<![\w.])(\d{1,3})\s*-?\s*amp(?:ere)?s?(?![A-Za-z0-9])",
               re.IGNORECASE),
    # "32 A", "32A", "32 a" - a lone letter is the unit symbol only where it
    # cannot be the article or a grade: at the end, before punctuation, or
    # before a curve, pole or device word ("2 A grade" is left alone)
    re.compile(r"(?<![\w.])(\d{1,3})\s*-?\s*[Aa](?![A-Za-z0-9])"
               r"(?=\s*(?:$|[,.;)]|(?i:(?:[bcd]|[bcd]-?curve|curve|sp|dp|tp|"
               r"mcb|mcbs|rccb|rcbo|isolator|switch|switches|socket|sockets|"
               r"breaker|fuse)\b)))"),
)


# Amazon Transcribe writes a spoken "32 amp C curve" as "32 AC curve" - the
# unit and the curve letter fused (live, 2026-09-29). Read that way only when
# "curve" follows, so "230V AC" or a bare "AC" is never touched.
_CURVE_FUSED = re.compile(r"(?<![\w.])(\d{1,3})\s*A([BCD])(?=[\s-]*curve\b)",
                          re.IGNORECASE)


# A rating spoken in words - "thirty two amp", "thirty-two amps", "sixteen
# ampere" - read as digits. Guarded hard: only 1 to 99, and only when the
# words sit directly before amp/amps/ampere. "thirty two" on its own, or
# "two 32 amp", is left exactly as written, because a bare number word is far
# more often a quantity than a rating.
_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_WORD_RATING = re.compile(
    r"(?<![\w-])(?:(?P<tens>" + "|".join(_TENS) + r")(?:[\s-]+(?P<unit>"
    + "|".join(_UNITS) + r"))?|(?P<small>" + "|".join({**_TEENS, **_UNITS})
    + r"))\s*-?\s*amp(?:ere)?s?(?![A-Za-z0-9])", re.IGNORECASE)


def _rating_words(text: str, notes: List[str]) -> str:
    def one(m: "re.Match") -> str:
        if m.group("tens"):
            value = _TENS[m.group("tens").lower()] + (
                _UNITS[m.group("unit").lower()] if m.group("unit") else 0)
        else:
            small = m.group("small").lower()
            value = _TEENS.get(small) or _UNITS[small]
        new = f"{value}A"
        notes.append(f"{' '.join(m.group(0).split())} -> {new}")
        return new
    return _WORD_RATING.sub(one, text)


def normalize_ratings(text: str) -> Tuple[str, List[str]]:
    """Write every explicit current rating as the catalogue does ("32A").

    Returns the text and a note per rewrite, so a caller that shows the
    owner their own words can say what changed. Wording only: which product
    the rating belongs to is still the matcher's decision.
    """
    notes: List[str] = []

    def one(m: "re.Match") -> str:
        new = f"{m.group(1)}A"
        if m.group(0) != new:
            notes.append(f"{' '.join(m.group(0).split())} -> {new}")
        return new

    def fused(m: "re.Match") -> str:
        new = f"{m.group(1)}A {m.group(2).upper()}"
        notes.append(f"{' '.join(m.group(0).split())} -> {new}")
        return new

    out = _CURVE_FUSED.sub(fused, text or "")
    out = _rating_words(out, notes)
    for pattern in _RATING_FORMS:
        out = pattern.sub(one, out)
    return out, notes


def _compound(text: str) -> str:
    rated, _ = normalize_ratings(text or "")
    return _COMPOUND.sub(r"\1\2", rated.lower())


def _tokens(text: str) -> List[str]:
    out = []
    for raw in _WORD.findall(_compound(text)):
        word = _SYNONYMS.get(raw, raw)
        if word in _STOPWORDS:
            continue
        out.append(word)
    return out


def _product_tokens(p: Product) -> List[str]:
    parts = [p.name, p.brand, p.category, p.specification or "",
             p.colour or "", p.length or "", p.skuId.replace("-", " ")]
    return _tokens(" ".join(parts))


def _is_count(token: str) -> bool:
    """A bare whole number. In an order line that is a quantity, never a
    product attribute: every specification in this catalogue carries a unit
    or a qualifier ("32a", "90m", "1.5", "1way", "4x4")."""
    return token.isdigit()


_SKU_PARTS = re.compile(r"[a-z0-9.]+")


def _sku_named(data: Dataset, query: str) -> List[Product]:
    """Products whose SKU id is written, part for part, in the query.

    Checked before free-text scoring because a SKU id is an identifier, not a
    description: "ACC-CONDUIT-20" names one conduit, and its "20" is part of
    the name rather than a count.
    """
    words = _SKU_PARTS.findall((query or "").lower())
    found = []
    for p in data.products.values():
        parts = _SKU_PARTS.findall(p.skuId.lower())
        n = len(parts)
        if n and any(words[i:i + n] == parts
                     for i in range(len(words) - n + 1)):
            found.append(p)
    return found


@dataclass
class MatchCandidate:
    product: Product
    score: float

    def as_dict(self) -> dict:
        p = self.product
        return {
            "skuId": p.skuId,
            "name": p.name,
            "brand": p.brand,
            "category": p.category,
            "specification": p.specification,
            "colour": p.colour,
            "length": p.length,
            "unit": p.unit,
            "uom": product_uom(p),
            "sellingPrice": p.sellingPrice,
            "isDefaultVariant": p.isDefaultVariant,
            "score": round(self.score, 3),
        }


@dataclass
class MatchResult:
    status: str
    requestedText: str
    skuId: Optional[str] = None
    resolvedBy: Optional[str] = None
    clarifyingAttribute: Optional[str] = None
    options: List[dict] = field(default_factory=list)
    candidates: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "requestedText": self.requestedText,
            "skuId": self.skuId,
            "resolvedBy": self.resolvedBy,
            "clarifyingAttribute": self.clarifyingAttribute,
            "options": self.options,
            "candidates": self.candidates,
        }


def _attr_matches(product: Product, attr: str, wanted: str) -> bool:
    if attr == "uom":
        # A unit is an exact fact about the SKU, not a description of it.
        # "2 boxes of switches" may only match a switch actually stocked in
        # boxes; there is no partial credit and no nearest unit.
        return product_uom(product) == normalize_uom(wanted)

    actual = getattr(product, attr, None)
    if actual is None:
        return False
    a, w = str(actual).lower(), str(wanted).lower()
    if attr == "specification":
        # "32 amp" should match "SP 32A C-Curve"; "1.5" should match "1.5 sqmm".
        want_tokens = [t for t in _tokens(w) if t]
        have = " ".join(_tokens(a))
        return all(t in have.split() or t in have for t in want_tokens)
    if attr == "length":
        return _tokens(a) == _tokens(w) or w in a
    return a == w or w in a


def search_catalog(
    data: Dataset,
    *,
    query: str = "",
    brand: Optional[str] = None,
    category: Optional[str] = None,
    specification: Optional[str] = None,
    colour: Optional[str] = None,
    length: Optional[str] = None,
    uom: Optional[str] = None,
    # High enough to hold every variant of a real product family. A cap that
    # bites would hand back an alphabetically-biased subset, and a truncated
    # candidate list becomes a clarification that omits the right answer.
    limit: int = 40,
) -> List[MatchCandidate]:
    """Score catalogue products against the stated attributes and free text.

    Explicit attributes are hard filters: asking for Finolex can never return
    Polycab. Free text only ranks what survives those filters.
    """
    filters = {"brand": brand, "category": category,
               "specification": specification, "colour": colour,
               "length": length, "uom": uom}
    filters = {k: v for k, v in filters.items() if v}

    pool: List[Product] = []
    for p in data.products.values():
        if all(_attr_matches(p, attr, val) for attr, val in filters.items()):
            pool.append(p)

    named = [p for p in _sku_named(data, query) if p in pool]
    if len(named) == 1:
        return [MatchCandidate(named[0], 1.0)]

    # Quantity never reaches product matching. The free text is scored on
    # its product words only; a bare number in it is the customer's count.
    all_tokens = [t for t in _tokens(query) if t]
    q_tokens = [t for t in all_tokens if not _is_count(t)]
    if all_tokens and not q_tokens and not filters:
        # Only numbers: there is no product description to match.
        return []
    scored: List[MatchCandidate] = []
    for p in pool:
        p_tokens = set(_product_tokens(p))
        if q_tokens:
            hits = sum(1 for t in q_tokens if t in p_tokens)
            if hits == 0 and not filters:
                continue
            score = hits / len(q_tokens)
        else:
            score = 1.0 if filters else 0.0
        scored.append(MatchCandidate(p, score))

    if q_tokens and scored:
        best = max(c.score for c in scored)
        # Keep only the strongest interpretations; a half-matching product is
        # noise, not a candidate worth asking the owner about.
        scored = [c for c in scored if c.score >= best - 1e-9]

    scored.sort(key=lambda c: (-c.score, c.product.skuId))
    return scored[:limit]


def resolve_product(
    data: Dataset,
    *,
    requested_text: str = "",
    brand: Optional[str] = None,
    category: Optional[str] = None,
    specification: Optional[str] = None,
    colour: Optional[str] = None,
    length: Optional[str] = None,
    uom: Optional[str] = None,
) -> MatchResult:
    """Turn a described product into exactly one SKU, or into a question.

    A stated `uom` is a hard filter like any other attribute. Asking for a box
    of something this shop only sells by the piece returns NOT_FOUND, which is
    the truthful answer: the shop does not stock that pack size, and quietly
    matching the loose product would be selling the customer something they
    did not ask for.
    """
    candidates = search_catalog(
        data, query=requested_text, brand=brand, category=category,
        specification=specification, colour=colour, length=length, uom=uom,
    )

    if not candidates:
        return MatchResult(status=NOT_FOUND, requestedText=requested_text)

    if len(candidates) == 1:
        return MatchResult(
            status=RESOLVED,
            requestedText=requested_text,
            skuId=candidates[0].product.skuId,
            resolvedBy="exact",
            candidates=[c.as_dict() for c in candidates],
        )

    # More than one real product fits, so the customer's wording does not
    # identify what they want. Ask.
    #
    # The shop's default variant is deliberately NOT used to break this tie.
    # On a quotation the wrong variant is a wrong price and a wrong delivery,
    # and a default that is merely displayed is still a choice the customer
    # never made. `isDefaultVariant` stays on the catalogue as metadata for
    # owner-facing features later; it has no vote here.
    products = [c.product for c in candidates]
    differing = [
        attr for attr in CLARIFIABLE_ATTRS
        if len({getattr(p, attr) for p in products}) > 1
    ]
    attr = differing[0] if differing else None

    if len(differing) == 1 and attr:
        # One attribute separates them, so the question can be about that
        # attribute alone: "which colour?"
        options = []
        seen = set()
        for c in candidates:
            value = getattr(c.product, attr)
            if value in seen:
                continue
            seen.add(value)
            options.append({"value": value, **c.as_dict()})
    else:
        # Several attributes differ - colour and coil length, say - so there is
        # no single clean question. Offer the actual products instead.
        options = [{"value": c.product.name, **c.as_dict()} for c in candidates]

    return MatchResult(
        status=AMBIGUOUS,
        requestedText=requested_text,
        clarifyingAttribute=attr,
        options=options,
        candidates=[c.as_dict() for c in candidates],
    )


def sku_exists(data: Dataset, skuId: str) -> bool:
    return skuId in data.products
